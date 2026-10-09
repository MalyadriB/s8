"""Download NSE's public end-of-day derivatives files (bhavcopy) for every NIFTY trading day since 2022-01-03 and keep only the NIFTY index-option rows.

Old format up to 2024-07-05, UDiFF format from 2024-07-08. Output: cache/nse_nifty_options_daily.csv (one row per contract per day:
date, expiry, strike, type, open, high, low, close, last, settle, contracts, oi, underlying).  Resumable; market data only, nothing is sent to any broker.
"""
from __future__ import annotations

import csv
import io
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "cache" / "nse_nifty_options_daily.csv"
DONE = HERE / "cache" / "nse_days_done.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
HEAD = ["date", "expiry", "strike", "type", "open", "high", "low", "close", "last", "settle", "contracts", "oi", "underlying"]


def trading_days() -> list[str]:
    days = set()
    for path in (HERE / "cache" / "nifty_1min_2022_to_2023-09-05.csv", Path(r"D:\zero\backend\data\exports\nifty_1min.csv")):
        with path.open(encoding="utf-8") as handle:
            next(handle)
            for line in handle:
                days.add(line[:10])
    days.add("2026-09-18")
    return sorted(d for d in days if d >= "2022-01-03")


def url_for(day: str) -> str:
    if day <= "2024-07-05":
        d = datetime.fromisoformat(day)
        return f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d.year}/{d.strftime('%b').upper()}/fo{d.strftime('%d%b%Y').upper()}bhav.csv.zip"
    return f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{day.replace('-', '')}_F_0000.csv.zip"


def get(day: str) -> bytes | None:
    for attempt in range(6):
        try:
            req = urllib.request.Request(url_for(day), headers={"User-Agent": UA, "Referer": "https://www.nseindia.com/"})
            return urllib.request.urlopen(req, timeout=60).read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            time.sleep(3 * (attempt + 1))
        except Exception:
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"could not download {day}")


def parse(day: str, data: bytes) -> list[list]:
    z = zipfile.ZipFile(io.BytesIO(data))
    rows = csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8"))
    out = []
    if day <= "2024-07-05":
        for r in rows:
            if r["SYMBOL"] == "NIFTY" and r["INSTRUMENT"] == "OPTIDX":
                expiry = datetime.strptime(r["EXPIRY_DT"], "%d-%b-%Y").date().isoformat()
                out.append([day, expiry, r["STRIKE_PR"], r["OPTION_TYP"], r["OPEN"], r["HIGH"], r["LOW"], r["CLOSE"], "", r["SETTLE_PR"], r["CONTRACTS"], r["OPEN_INT"], ""])
    else:
        for r in rows:
            if r["TckrSymb"] == "NIFTY" and r["FinInstrmTp"] == "IDO":
                out.append([day, r["FininstrmActlXpryDt"], r["StrkPric"], r["OptnTp"], r["OpnPric"], r["HghPric"], r["LwPric"], r["ClsPric"], r["LastPric"], r["SttlmPric"], r["TtlTradgVol"], r["OpnIntrst"], r["UndrlygPric"]])
    return out


def main() -> None:
    done = set(DONE.read_text().split()) if DONE.exists() else set()
    new_file = not OUT.exists()
    days = trading_days()
    print(len(days), "trading days;", len(done), "already done", flush=True)
    with OUT.open("a", newline="", encoding="utf-8") as handle, DONE.open("a") as marker:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(HEAD)
        for i, day in enumerate(days):
            if day in done:
                continue
            try:
                data = get(day)
            except RuntimeError as error:
                print("FAILED (will retry on the next run):", error, flush=True)  # not marked done, so a re-run picks it up
                time.sleep(20)
                continue
            if data is None:
                marker.write(f"{day}\n")
                marker.flush()
                print(day, "no file (404)", flush=True)
                continue
            rows = parse(day, data)
            writer.writerows(rows)
            handle.flush()
            marker.write(f"{day}\n")
            marker.flush()
            if i % 25 == 0:
                print(day, len(rows), "rows", flush=True)
            time.sleep(0.35)
    print("done", flush=True)


if __name__ == "__main__":
    main()

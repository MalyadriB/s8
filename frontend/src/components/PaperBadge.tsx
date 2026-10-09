/** Marks every screen that shows paper-trading results. Paper trades are simulated: nothing here is a real order. */
export function PaperBadge({ title = 'PAPER trading - simulated fills, no real order is ever placed' }: { title?: string | null }) {
  // null: no tooltip (a badge repeated in every cell of a grid, where it would only pop up over the numbers)
  return (
    <span className="paper-badge" title={title ?? undefined}>
      PAPER
    </span>
  )
}

export function formatAge(seconds: number): string {
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return Math.floor(seconds / 60) + ' min'
  if (seconds < 86400) return Math.floor(seconds / 3600) + ' h'
  return Math.floor(seconds / 86400) + ' d'
}
export function formatDuration(seconds: number): string {
  if (seconds < 60) return 'less than a minute'
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60)
  return h ? `${h} h ${String(m).padStart(2, '0')} min` : `${m} min`
}
export function clampTitle(text: string, max = 96): string {
  if (text.length <= max) return text
  const cut = text.slice(0, max); const space = cut.lastIndexOf(' ')
  return (space > max * 0.6 ? cut.slice(0, space) : cut).trimEnd() + '…'
}

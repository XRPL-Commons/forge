import assert from 'node:assert/strict'

// Modal footers put the primary action at the far right: it sits right of the
// secondary button and its right edge meets the footer's right edge.
export async function assertPrimaryFarRight(dialog, { primary, secondary }) {
  const footer = await dialog.locator('[data-dialog-footer]').boundingBox()
  const first = await dialog.getByRole('button', { name: secondary, exact: true }).boundingBox()
  const last = await dialog.getByRole('button', { name: primary, exact: true }).boundingBox()
  assert.ok(footer && first && last, 'footer and both buttons are visible')
  assert.ok(last.x >= first.x + first.width, `${primary} sits right of ${secondary}`)
  assert.ok(Math.abs(last.x + last.width - (footer.x + footer.width)) <= 1, `${primary} ends at the footer's right edge`)
}

// Below 480px the footer stacks full-width buttons with the primary on top.
export async function assertPhoneStack(dialog, { primary, secondary }) {
  const footer = await dialog.locator('[data-dialog-footer]').boundingBox()
  const first = await dialog.getByRole('button', { name: secondary, exact: true }).boundingBox()
  const last = await dialog.getByRole('button', { name: primary, exact: true }).boundingBox()
  assert.ok(footer && first && last, 'footer and both buttons are visible')
  assert.ok(last.y + last.height <= first.y, `${primary} stacks above ${secondary}`)
  for (const box of [first, last]) assert.ok(Math.abs(box.width - footer.width) <= 1, 'stacked buttons are full width')
}

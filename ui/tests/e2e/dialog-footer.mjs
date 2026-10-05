import assert from 'node:assert/strict'

// Reads the footer and its two buttons in one frame, after layout settles, so
// a resize or font swap between separate reads cannot skew the comparison.
async function measure(dialog, { primary, secondary }) {
  const footer = dialog.locator('[data-dialog-footer]')
  await footer.waitFor()
  const [p, s] = [primary, secondary].map(name => dialog.getByRole('button', { name, exact: true }))
  await p.waitFor()
  await s.waitFor()
  const primaryHandle = await p.elementHandle()
  const secondaryHandle = await s.elementHandle()
  return footer.evaluate(async (el, [pb, sb]) => {
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    const box = node => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width } }
    const style = getComputedStyle(el)
    const f = box(el)
    const dialogEl = el.closest('[role="dialog"]')
    const overflowing = [...(dialogEl?.querySelectorAll('*') ?? [])]
      .filter(node => node.scrollWidth > node.clientWidth + 1 && getComputedStyle(node).overflowX !== 'visible')
      .map(node => `${node.tagName.toLowerCase()}.${String(node.className).slice(0, 60)} ${node.scrollWidth}>${node.clientWidth}`)
    return {
      footer: f,
      contentLeft: f.left + parseFloat(style.paddingLeft),
      contentRight: f.right - parseFloat(style.paddingRight),
      primary: box(pb),
      secondary: box(sb),
      dialog: dialogEl ? box(dialogEl) : null,
      viewport: innerWidth,
      overflowing,
    }
  }, [primaryHandle, secondaryHandle])
}

// Modal footers put the primary action at the far right: it starts after the
// secondary button ends, and its right edge meets the footer's content box.
export async function assertPrimaryFarRight(dialog, names) {
  const m = await measure(dialog, names)
  const detail = JSON.stringify(m)
  assert.ok(m.primary.left >= m.secondary.right, `${names.primary} sits right of ${names.secondary}: ${detail}`)
  assert.ok(Math.abs(m.primary.right - m.contentRight) <= 2, `${names.primary} ends at the footer's right edge: ${detail}`)
}

// Below 480px the footer stacks full-width buttons with the primary on top.
export async function assertPhoneStack(dialog, names) {
  const m = await measure(dialog, names)
  const detail = JSON.stringify(m)
  assert.ok(m.primary.bottom <= m.secondary.top, `${names.primary} stacks above ${names.secondary}: ${detail}`)
  const width = m.contentRight - m.contentLeft
  for (const b of [m.primary, m.secondary]) assert.ok(Math.abs(b.width - width) <= 2, `stacked buttons are full width: ${detail}`)
}

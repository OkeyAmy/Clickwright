"""Resolve a stable selector for whatever sits under a coordinate.

Computer use returns coordinates. Coordinates cannot be replayed — a compiled
playbook needs something that survives a re-render. This module runs in the
page and returns an ordered list of candidates, most durable first, so the
runtime can fall through when the top one stops matching.
"""

from __future__ import annotations

# Ordered by how well each survives a redesign:
#   data-testid > id > name > accessible name > label text > tag+nth
RESOLVE_JS = """
(point) => {
  const el = document.elementFromPoint(point.x, point.y);
  if (!el) return null;

  // A click lands on whatever chrome wraps the control — a span inside a
  // button, an svg inside a link. Resolve the control, not the chrome: the
  // span's positional selector dies on the next render, the button's does not.
  const target = el.closest('button, a, [role="button"], [role="tab"], [role="menuitem"], summary') || el;

  const esc = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/["\\\\]/g, '\\\\$&'));
  const cands = [];

  for (const attr of ['data-testid', 'data-test', 'data-qa']) {
    const v = target.getAttribute(attr);
    if (v) cands.push(`[${attr}="${v}"]`);
  }
  if (target.id) cands.push(`#${esc(target.id)}`);

  const name = target.getAttribute('name');
  if (name) cands.push(`${target.tagName.toLowerCase()}[name="${name}"]`);

  const label = (target.getAttribute('aria-label') || '').trim();
  if (label) cands.push(`${target.tagName.toLowerCase()}[aria-label="${label}"]`);

  const value = (target.getAttribute('value') || '').trim();
  if (value && ['submit', 'button'].includes((target.getAttribute('type') || '').toLowerCase())) {
    cands.push(`input[type="${target.getAttribute('type')}"][value="${value}"]`);
  }

  const role = (target.getAttribute('role') || '').toLowerCase();
  const clickableTag = ['BUTTON', 'A'].includes(target.tagName) ||
    ['button', 'tab', 'menuitem'].includes(role);
  const text = (target.textContent || '').trim().slice(0, 60);
  if (text && clickableTag) {
    cands.push(`${target.tagName.toLowerCase()}:has-text("${text}")`);
  }

  // last resort: positional
  const parent = target.parentElement;
  if (parent) {
    const sibs = [...parent.children].filter((c) => c.tagName === target.tagName);
    const idx = sibs.indexOf(target) + 1;
    cands.push(`${target.tagName.toLowerCase()}:nth-of-type(${idx})`);
  }

  return {
    candidates: cands,
    tag: target.tagName.toLowerCase(),
    type: target.getAttribute('type'),
    accessible_name: label || target.getAttribute('placeholder') || null,
    text: text || null,
  };
}
"""


def to_selector(resolved: dict | None):
    """Turn the page's answer into a Selector, or None if nothing was there."""
    from app.connectors.models import Selector

    if not resolved or not resolved.get("candidates"):
        return None
    candidates = resolved["candidates"]
    return Selector(
        primary=candidates[0],
        fallbacks=candidates[1:],
        accessible_name=resolved.get("accessible_name"),
        text=resolved.get("text"),
    )

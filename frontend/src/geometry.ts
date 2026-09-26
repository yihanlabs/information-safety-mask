import type { EditState, Mask, Rect } from './types';
export function boundedRect(rect: Rect, width: number, height: number): Rect {
  const w = Math.max(1, Math.min(width, rect.width)), h = Math.max(1, Math.min(height, rect.height));
  return { x: Math.max(0, Math.min(width - w, rect.x)), y: Math.max(0, Math.min(height - h, rect.y)), width: w, height: h };
}
export function fromPoints(a: { x: number; y: number }, b: { x: number; y: number }): Rect {
  return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.abs(a.x - b.x), height: Math.abs(a.y - b.y) };
}
export function changeMask(edits: EditState, mask: Mask, box: Rect): EditState {
  const copy = structuredClone(edits);
  if (mask.source === 'manual') copy.manual = copy.manual.map(item => item.id === mask.id ? { ...item, box } : item);
  else copy.overrides[mask.id] = box;
  return copy;
}
export function removeMask(edits: EditState, mask: Mask): EditState {
  const copy = structuredClone(edits);
  if (mask.source === 'manual') copy.manual = copy.manual.filter(item => item.id !== mask.id);
  else copy.excluded = [...new Set([...copy.excluded, mask.id])];
  return copy;
}

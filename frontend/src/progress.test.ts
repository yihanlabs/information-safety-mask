import { describe, expect, it } from 'vitest';
import { elapsedLabel, estimateLabel } from './ProcessingStatus';

describe('honest waiting-time labels', () => {
  it('keeps elapsed minutes and seconds legible for long images', () => {
    expect(elapsedLabel(0)).toBe('00:00');
    expect(elapsedLabel(125.9)).toBe('02:05');
    expect(elapsedLabel(3605)).toBe('60:05');
  });
  it('rounds estimates to broad ranges and never promises zero seconds', () => {
    expect(estimateLabel(1, 3)).toBe('约 5 秒');
    expect(estimateLabel(12, 33)).toBe('约 15–35 秒');
    expect(estimateLabel(30, 130)).toBe('约 30 秒–3 分钟');
    expect(estimateLabel(70, 185)).toBe('约 2–4 分钟');
  });
});

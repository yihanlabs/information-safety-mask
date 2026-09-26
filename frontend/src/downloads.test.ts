import { describe, expect, it, vi } from 'vitest';
import { downloadName, downloadSequential, type DownloadItem } from './downloads';

vi.mock('./api', () => ({ body: vi.fn(), request: vi.fn() }));

describe('individual PNG downloads', () => {
  it('fixes revisions, runs sequentially and continues after a per-file failure', async () => {
    const items: DownloadItem[] = [1, 2, 3].map(number => ({ id: String(number), number, revision: 4, status: 'waiting' }));
    const events: DownloadItem[] = [], generated: number[] = [], dispatched: number[] = [];
    let inFlight = 0;
    const makeFile = async (item: DownloadItem) => {
      expect(inFlight++).toBe(0);
      await Promise.resolve();
      generated.push(item.number); inFlight--;
      expect(item.revision).toBe(4);
      if (item.number === 1) items[2].revision = 99;
      if (item.number === 2) throw new Error('确认已失效');
      return new Blob(['png']);
    };
    const count = await downloadSequential(items, item => events.push(item), makeFile, (_, item) => { dispatched.push(item.number); });
    expect(count).toBe(2);
    expect(generated).toEqual([1, 2, 3]); expect(dispatched).toEqual([1, 3]);
    expect(events.filter(item => item.status === 'failed')[0].error).toBe('确认已失效');
    expect(events.filter(item => item.status === 'submitted')).toHaveLength(2);
    expect(downloadName(items[0])).toBe('sanitized_001.png');
  });

  it('a single retry generates only its selected confirmed revision', async () => {
    const make = vi.fn(async () => new Blob(['png'])), send = vi.fn();
    const item: DownloadItem = { id: 'three', revision: 7, number: 3, status: 'failed', error: 'old error' };
    const events: DownloadItem[] = [];
    await downloadSequential([item], entry => events.push(entry), make, send);
    expect(make).toHaveBeenCalledTimes(1); expect(send).toHaveBeenCalledTimes(1);
    expect(events.at(-1)).toMatchObject({ status: 'submitted', revision: 7, error: undefined });
  });
});

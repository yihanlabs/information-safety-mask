import { body, json } from './api';

export interface DownloadItem {
  id: string; revision: number; number: number;
  status: 'waiting' | 'generating' | 'submitted' | 'failed';
  error?: string;
  progress?: string;
}
export const downloadName = (item: Pick<DownloadItem, 'number'>) => `sanitized_${String(item.number).padStart(3, '0')}.png`;

interface ExportJob { id: string; status: 'queued' | 'generating' | 'ready' | 'failed'; completed: number; total: number | null; error: string | null }
async function generate(item: DownloadItem, progress: (text: string) => void): Promise<string | Blob> {
  let job = await json<ExportJob>('exports', { method: 'POST', ...body({
    images: [{ id: item.id, revision: item.revision }], format: 'png',
  }) });
  while (job.status !== 'ready') {
    if (job.status === 'failed') throw new Error(job.error || 'PNG 生成未完成，请重试');
    progress(job.total ? `已处理 ${job.completed} / ${job.total} 块` : '等待生成');
    await new Promise(resolve => setTimeout(resolve, 600));
    job = await json<ExportJob>('exports/' + job.id);
  }
  return (await json<{ url: string }>(`exports/${job.id}/ticket`, { method: 'POST' })).url;
}

function dispatch(file: Blob | string, item: DownloadItem) {
  const url = typeof file === 'string' ? file : URL.createObjectURL(file), link = document.createElement('a');
  try {
    link.href = url; link.download = downloadName(item);
    document.body.appendChild(link); link.click();
  } finally {
    link.remove(); if (typeof file !== 'string') setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
}

// A browser may block automatic downloads. "submitted" never means saved to disk.
export async function downloadSequential(
  items: DownloadItem[], update: (item: DownloadItem, index: number) => void,
  makeFile = generate, sendFile = dispatch,
) {
  const snapshots = items.map(item => ({ ...item }));
  let submitted = 0;
  for (const [index, item] of snapshots.entries()) {
    update({ ...item, status: 'generating', error: undefined }, index);
    try {
      const blob = await makeFile(item, progress => update({ ...item, status: 'generating', progress }, index));
      sendFile(blob, item);
      submitted++;
      update({ ...item, status: 'submitted', error: undefined }, index);
    } catch (error) {
      update({ ...item, status: 'failed', error: error instanceof Error ? error.message : '生成未完成，请重试' }, index);
    }
  }
  return submitted;
}

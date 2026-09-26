import { useState } from 'react';
import { ChevronDown, Loader2 } from 'lucide-react';
import type { ImageRecord, MemoryStatus } from './types';

export function elapsedLabel(seconds: number) {
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 60).toString().padStart(2, '0')}:${(value % 60).toString().padStart(2, '0')}`;
}

export function estimateLabel(low: number, high: number) {
  const rounded = (value: number) => Math.max(5, Math.ceil(value / 5) * 5);
  if (high < 60) {
    const a = rounded(low), b = rounded(high);
    return a === b ? `约 ${b} 秒` : `约 ${a}–${b} 秒`;
  }
  const b = Math.ceil(high / 60);
  if (low < 60) return `约 ${rounded(low)} 秒–${b} 分钟`;
  const a = Math.ceil(low / 60);
  return a === b ? `约 ${b} 分钟` : `约 ${a}–${b} 分钟`;
}

const steps = ['准备图片', '准备模型', '寻找文字', '识别文字', '分析敏感内容'];

export default function ProcessingStatus({ image, disconnected, memory }: { image: ImageRecord; disconnected: boolean; memory?: MemoryStatus }) {
  const [expanded, setExpanded] = useState(false);
  if (!['queued', 'processing'].includes(image.status)) return null;
  const progress = image.progress;
  const queued = image.status === 'queued';
  const countable = progress && (['preparing', 'recognizing', 'analyzing'].includes(progress.stage) || progress.unit === 'tile') && !!progress.total;
  const unit = progress?.unit === 'tile' || progress?.unit === 'strip' ? '块' : '行';
  const percent = countable ? progress.percent : null;
  const label = queued ? memory?.waiting_image_id ? '等待释放内存后继续队列' : `等待识别${image.queue_position ? ` · 排队第 ${image.queue_position} 张` : ''}` : progress?.label || '正在本地识别';
  const estimate = progress?.eta_min_seconds != null && progress.eta_max_seconds != null
    ? estimateLabel(progress.eta_min_seconds, progress.eta_max_seconds) : null;
  const timing = disconnected ? '进度暂时无法更新，正在重试连接'
    : queued ? '按顺序逐张处理，轮到本张后开始计时'
    : progress?.overdue ? '已超出估计时间，本阶段尚未返回结果'
    : estimate ? `${progress?.eta_scope === 'stage' ? '本阶段' : '预计'}还需 ${estimate}${progress?.estimate_basis === 'rough' ? '（粗估）' : '（估计）'}`
    : '正在估算时间…';

  return <div className={'processing-card' + (disconnected ? ' disconnected' : '')} aria-label="图片识别进度">
    <strong title={memory?.message || undefined}><Loader2 size={13} className={disconnected || queued ? '' : 'spin'} />{label}{!queued && memory?.mode === 'saving' && <small aria-label={memory.message || undefined}> · 省内存</small>}</strong>
    <div className="processing-track" role="progressbar" aria-label="当前阶段进度" aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={percent ?? undefined} aria-valuetext={disconnected ? '连接中断，进度待更新' : countable ? `已完成 ${progress.completed} / ${progress.total} ${unit}` : label}>
      {countable ? <i style={{ width: `${percent}%` }} /> : !queued && <i className="indeterminate" />}
    </div>
    <div className="processing-details"><span>{queued ? '已等待 ' : '已用 '}{elapsedLabel(queued ? progress?.waiting_seconds || 0 : progress?.elapsed_seconds || 0)}</span>
      {countable && <span>已完成 {progress.completed} / {progress.total} {unit}</span>}
      <span className="processing-estimate" role="status">{timing}</span>
    </div>
    <button className="icon-button" aria-label="识别步骤详情" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}><ChevronDown size={14} /></button>
    {expanded && !queued && <ol className="processing-steps" aria-label="识别步骤">{steps.map((step, i) => <li key={step}
      className={(progress?.step || 0) > i + 1 ? 'complete' : progress?.step === i + 1 ? 'current' : ''}
      aria-current={progress?.step === i + 1 ? 'step' : undefined}><span />{step}</li>)}
      {memory?.message && <li>{memory.message}</li>}
      {memory?.workspace_estimate_known === false && <li>本阶段内存需求随内容变化，正在监测。</li>}
    </ol>}
  </div>;
}

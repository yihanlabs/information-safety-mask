import { useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { body, json } from './api';
import type { Health, RuntimeStatus } from './types';

export default function SettingsDialog({ health, onClose, onUpdate }: {
  health?: Health; onClose: () => void; onUpdate: (runtime: RuntimeStatus) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [saving, setSaving] = useState(false);
  const [choice, setChoice] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const runtime = health?.runtime;
  useEffect(() => { dialog.current?.showModal(); }, []);
  async function change(enabled: boolean) {
    setSaving(true); setChoice(enabled); setError('');
    try { onUpdate(await json<RuntimeStatus>('performance', { method: 'PUT', ...body({ mode: enabled ? 'high_performance' : 'low_impact' }) })); }
    catch (failure) { setError((failure as Error).message); }
    finally { setSaving(false); setChoice(null); }
  }
  return <dialog ref={dialog} className="settings-dialog" onCancel={onClose} onClick={e => { if (e.target === dialog.current) {
    const r = dialog.current.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) onClose();
  } }}>
    <div className="dialog-heading"><h2>设置</h2><button className="icon-button" aria-label="关闭设置" onClick={onClose}><X size={18} /></button></div>
    <label className="performance-choice"><span><strong>高占用模式</strong><small>最高使用 80% CPU，加快识别；风扇声音可能增大。</small></span>
      <input type="checkbox" role="switch" aria-label="高占用模式" disabled={saving || !runtime} checked={choice ?? runtime?.requested_mode === 'high_performance'} onChange={e => change(e.target.checked)} />
    </label>
    <p className="setting-description">{runtime?.pending ? '本张完成后生效' : runtime?.active_mode === 'high_performance' ? '当前使用高占用模式' : '当前使用低占用模式'}。选择会保存在本机，切换后的首次识别需重新加载模型。</p>
    {(error || runtime?.warning || runtime?.fallback_reason) && <p className="setting-error" role="alert">{[error, runtime?.warning, runtime?.fallback_reason].filter(Boolean).join('；')}</p>}
    <details className="runtime-details"><summary>运行详情</summary><dl>
      <dt>内存处理</dt><dd>{health?.memory?.mode === 'saving' ? '已启用省内存处理，可能需要更长时间' : '常规处理，内存偏紧时自动调整'}</dd>
      <dt>可用内存</dt><dd>{health?.memory ? `${(health.memory.available_bytes/1024**3).toFixed(1)} GiB` : '读取中'}</dd>
      <dt>系统剩余可分配额度</dt><dd>{health?.memory?.commit_available_bytes != null ? `${(health.memory.commit_available_bytes/1024**3).toFixed(1)} GiB` : '无法读取'}</dd>
      {health?.memory?.processing && health.memory.workspace_estimate_known === false && <><dt>阶段内存估计</dt><dd>需求随图片内容变化，正在监测可用内存。</dd></>}
      <dt>CPU 预算</dt><dd>{runtime?.cpu_cap_applied ? `${runtime.cpu_budget_percent}%（已生效）` : '尚未生效'}</dd>
      <dt>识别线程</dt><dd>{runtime?.threads ?? '尚未启动'}</dd>
      <dt>计算方式</dt><dd>{runtime?.backend === 'accelerated' ? 'CPU 加速' : runtime?.backend === 'compatible' ? '兼容模式' : '等待识别'}</dd>
      <dt>低优先级</dt><dd>{runtime?.below_normal == null ? '尚未启动' : runtime.below_normal ? '已生效' : '未生效'}</dd>
      <dt>节能调度</dt><dd>{runtime?.ecoqos == null ? '尚未启动' : runtime.ecoqos ? '开启' : '关闭'}</dd>
    </dl></details>
  </dialog>;
}

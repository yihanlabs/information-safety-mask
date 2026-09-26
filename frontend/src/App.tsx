import { useCallback, useEffect, useRef, useState } from 'react';
import './memory.css';
import { AlertCircle, ArrowDownToLine, ArrowLeft, ArrowRight, Check, CheckCheck, CheckCircle2, Circle, FileImage, FolderOpen, ImagePlus, Loader2, PanelLeft, PanelRight, Plus, Settings, Trash2, X } from 'lucide-react';
import { body, json, request } from './api';
import { changeMask, removeMask } from './geometry';
import Editor from './Editor';
import RulesPanel from './RulesPanel';
import SettingsDialog from './SettingsDialog';
import ProcessingStatus from './ProcessingStatus';
import { downloadName, downloadSequential } from './downloads';
import type { DownloadItem } from './downloads';
import type { EditState, Health, ImageRecord, Mask, Rect, RuleSet } from './types';
type History = { past: EditState[]; future: EditState[] };
const clone = <T,>(value: T): T => structuredClone(value);

export default function App() {
  const [queueCollapsed, setQueueCollapsed] = useState(false);
  const [rulesCollapsed, setRulesCollapsed] = useState(false);
  const [focusMode, setFocusMode] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showDownloads, setShowDownloads] = useState(false);
  const [rulesTab, setRulesTab] = useState<'rules' | 'masks'>('rules');
  const [focusTarget, setFocusTarget] = useState<{ id: string; nonce: number } | null>(null);
  const [images, setImages] = useState<ImageRecord[]>([]);
  const [rules, setRules] = useState<RuleSet>({ categories: ['person', 'identity', 'address', 'phone'], custom_fields: [] });
  const [health, setHealth] = useState<Health>();
  const [activeId, setActiveId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [importing, setImporting] = useState('');
  const [downloads, setDownloads] = useState<DownloadItem[]>([]);
  const [downloadProgress, setDownloadProgress] = useState('');
  const [error, setError] = useState('');
  const [disconnected, setDisconnected] = useState(false);
  const [toast, setToast] = useState('');
  const [dragging, setDragging] = useState(false);
  const [, setHistoryTick] = useState(0);
  const filesInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const busyRef = useRef(false);
  const pendingImports = useRef<File[]>([]);
  const refreshId = useRef(0);
  const history = useRef<Record<string, History>>({});
  const urlCache = useRef<Record<string, string>>({});
  const loadingUrls = useRef(new Set<string>());
  const imagesRef = useRef<ImageRecord[]>([]);
  imagesRef.current = images;
  const active = images.find(image => image.id === activeId) || null;
  const ready = active && active.preview_ready && !['queued', 'processing'].includes(active.status);
  const retryable = active && ['error', 'waiting_memory'].includes(active.status);
  const reviewWarning = (ready || retryable) && active?.message !== '识别完成，请检查遮盖后确认' ? active?.message : null;
  const confirmed = images.filter(image => image.confirmed);
  const pending = images.filter(image => ['queued', 'processing'].includes(image.status)).length;
  const activeHistory = active ? history.current[active.id] : undefined;
  const refresh = useCallback(async () => {
    const id = ++refreshId.current;
    try {
      const [next, status] = await Promise.all([json<ImageRecord[]>('images'), json<Health>('health')]);
      if (id !== refreshId.current) return;
      setDisconnected(false);
      setImages(next);
      setHealth(status);
      setActiveId(current => next.some(item => item.id === current) ? current : next[0]?.id || null);
    } catch (failure) {
      if (id === refreshId.current) setDisconnected(true);
      throw failure;
    }
  }, []);
  useEffect(() => {
    let mounted = true;
    Promise.all([json<RuleSet>('settings'), json<Health>('health')]).then(([next, status]) => {
      if (mounted) { setRules(next); setHealth(status); }
    }).catch(e => { if (mounted) setError(e.message); });
    refresh().catch(e => { if (mounted) setError(e.message); });
    let polling = false;
    const timer = setInterval(() => {
      if (!polling && !busyRef.current) {
        polling = true;
        refresh().catch(() => {}).finally(() => { polling = false; });
      }
    }, 1500);
    return () => { mounted = false; clearInterval(timer); };
  }, [refresh]);
  useEffect(() => { setSelected(null); }, [activeId]);
  const imageIds = images.map(image => image.id + ':' + image.preview_ready).join(',');
  useEffect(() => {
    const ids = new Set(images.map(image => image.id));
    for (const id of Object.keys(urlCache.current)) if (!ids.has(id)) {
      URL.revokeObjectURL(urlCache.current[id]); delete urlCache.current[id];
    }
    for (const image of images) {
      if (!image.preview_ready || urlCache.current[image.id] || loadingUrls.current.has(image.id)) continue;
      loadingUrls.current.add(image.id);
      request('images/' + image.id + '/preview?size=256').then(response => response.blob()).then(blob => {
        if (imagesRef.current.some(item => item.id === image.id)) {
          urlCache.current[image.id] = URL.createObjectURL(blob); setUrls({ ...urlCache.current });
        }
      }).catch(() => {}).finally(() => loadingUrls.current.delete(image.id));
    }
    setUrls({ ...urlCache.current });
  }, [imageIds]);
  useEffect(() => () => { Object.values(urlCache.current).forEach(URL.revokeObjectURL); }, []);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 4200); return () => clearTimeout(timer);
  }, [toast]);

  async function run(action: () => Promise<void>): Promise<boolean> {
    if (busyRef.current) return false;
    busyRef.current = true; setBusy(true); setError(''); refreshId.current++;
    try { await action(); await refresh(); return true; }
    catch (e) { setError(e instanceof Error ? e.message : '操作未完成，请重试'); await refresh().catch(() => {}); return false; }
    finally {
      busyRef.current = false; setBusy(false);
      if (pendingImports.current.length) {
        const files = pendingImports.current.splice(0);
        void importFiles(files);
      }
    }
  }
  async function importFiles(list: FileList | File[] | null) {
    const files = Array.from(list || []);
    if (!files.length) return;
    if (busyRef.current) { pendingImports.current.push(...files); return; }
    await run(async () => {
      let count = 0;
      const failures: string[] = [];
      for (let index = 0; index < files.length; index++) {
        const file = files[index];
        if (!/\.(png|jpe?g|webp|bmp)$/i.test(file.name)) { failures.push(file.name + '：格式不支持'); continue; }
        setImporting('正在导入 ' + (index + 1) + ' / ' + files.length);
        try {
          const added = await json<ImageRecord>('images', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Image-Name': encodeURIComponent(file.name) }, body: file });
          setImages(previous => previous.some(item => item.id === added.id) ? previous : [...previous, added]); setActiveId(current => current || added.id); count++;
        } catch (e) { failures.push(file.name + '：' + (e as Error).message); }
      }
      setImporting('');
      if (failures.length) setError(failures.slice(0, 4).join('；') + (failures.length > 4 ? '…' : ''));
      if (count) setToast('已导入 ' + count + ' 张图片，正在本地处理');
    });
    setImporting('');
    if (filesInput.current) filesInput.current.value = '';
    if (folderInput.current) folderInput.current.value = '';
  }
  async function saveRules(next: RuleSet) {
    if (busyRef.current) return false;
    const previous = rules;
    setRules(next);
    const saved = await run(async () => { setRules(await json<RuleSet>('settings', { method: 'PUT', ...body(next) })); });
    if (!saved) setRules(previous);
    return saved;
  }
  async function edit(next: EditState, mode: 'new' | 'undo' | 'redo' = 'new') {
    if (!active) return;
    const image = active;
    await run(async () => {
      await json<ImageRecord>('images/' + image.id + '/edits', { method: 'PUT', ...body({ revision: image.revision, edits: next }) });
      const stack = history.current[image.id] ||= { past: [], future: [] };
      if (mode === 'new') { stack.past.push(clone(image.edits)); stack.future = []; }
      if (mode === 'undo') { stack.past.pop(); stack.future.push(clone(image.edits)); }
      if (mode === 'redo') { stack.future.pop(); stack.past.push(clone(image.edits)); }
      setHistoryTick(value => value + 1);
    });
  }
  function undo() { if (activeHistory?.past.length) edit(clone(activeHistory.past.at(-1)!), 'undo'); }
  function redo() { if (activeHistory?.future.length) edit(clone(activeHistory.future.at(-1)!), 'redo'); }
  function remove(mask?: Mask) {
    const target = mask || active?.masks.find(item => item.id === selected);
    if (target && active) { edit(removeMask(active.edits, target)); setSelected(null); }
  }
  function add(box: Rect) {
    if (!active) return;
    const id = 'manual-' + crypto.randomUUID();
    edit({ ...clone(active.edits), manual: [...active.edits.manual, { id, box }] }); setSelected(id); setRulesTab('masks');
  }
  function confirm() {
    if (!active) return;
    const index = images.findIndex(item => item.id === active.id);
    run(async () => {
      await json('images/' + active.id + '/confirm', { method: 'POST', ...body({ revision: active.revision, reviewed: true }) });
      setToast('已确认当前图片');
      const next = images.slice(index + 1).find(item => !item.confirmed);
      if (next) setActiveId(next.id);
    });
  }
  function download(single = false, retry?: DownloadItem) {
    const chosen = retry ? [retry] : single && active ? [active] : confirmed;
    if (!chosen.length) return;
    const snapshots: DownloadItem[] = chosen.map(({ id, revision, number }) => ({ id, revision, number, status: 'waiting' }));
    run(async () => {
      if (!retry) setDownloads(snapshots);
      setShowDownloads(true);
      try {
        const count = await downloadSequential(snapshots, (entry, index) => {
        if (entry.status === 'generating') setDownloadProgress(`正在生成第 ${index + 1} / ${snapshots.length} 张${entry.progress ? ' · ' + entry.progress : ''}`);
          setDownloads(current => current.map(item => item.id === entry.id && item.revision === entry.revision ? entry : item));
        });
        if (count === snapshots.length) setShowDownloads(false);
        setToast(count ? `已提交 ${count} 张 PNG 至浏览器下载，请查看下载列表` : '图片生成未完成，请查看下载详情');
      } finally { setDownloadProgress(''); }
    });
  }
  function removeImage(image: ImageRecord) {
    run(async () => { await request('images/' + image.id, { method: 'DELETE' }); delete history.current[image.id]; });
  }
  function selectMask(id: string) {
    setSelected(id); setFocusTarget({ id, nonce: Date.now() }); setRulesTab('masks');
  }
  const controls = active && <>
    <button className="tool" aria-label="上一张" disabled={images[0]?.id === active.id} onClick={() => setActiveId(images[images.findIndex(item => item.id === active.id)-1].id)}><ArrowLeft size={16} /></button>
    <span className="image-index">{images.findIndex(item => item.id === active.id)+1}/{images.length}</span>
    <button className="tool" aria-label="下一张" disabled={images.at(-1)?.id === active.id} onClick={() => setActiveId(images[images.findIndex(item => item.id === active.id)+1].id)}><ArrowRight size={16} /></button>
    <button className="tool" title="导出本张" aria-label="导出本张" disabled={busy || !active.confirmed} onClick={() => download(true)}><ArrowDownToLine size={17} /></button>
    <button className={'button primary confirm-button ' + (active.confirmed ? 'confirmed-button' : '')} disabled={busy || !ready || active.confirmed || !!health?.settings_error} onClick={confirm}>
      {active.confirmed ? <CheckCheck size={16} /> : <Check size={16} />}{active.confirmed ? '已确认' : images.at(-1)?.id === active.id ? '确认此图片' : '确认并下一张'}
    </button>
  </>;
  return <div className={'app' + (focusMode ? ' focused' : '')}
    onDragOver={e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragging(true); } }}
    onDragLeave={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false); }}
    onDrop={e => { e.preventDefault(); setDragging(false); if (!busy) importFiles(e.dataTransfer.files); }}>
    <input ref={filesInput} type="file" accept=".png,.jpg,.jpeg,.webp,.bmp" multiple hidden onChange={e => importFiles(e.target.files)} aria-label="选择图片文件" />
    <input ref={folderInput} type="file" multiple hidden {...({ webkitdirectory: '' } as object)} onChange={e => importFiles(e.target.files)} aria-label="选择图片文件夹" />
    <header className="app-header">
      <div className="header-start">
        <button className="icon-button" aria-label={queueCollapsed ? '展开图片队列' : '收起图片队列'} aria-pressed={!queueCollapsed} onClick={() => setQueueCollapsed(!queueCollapsed)}><PanelLeft size={18} /></button>
        <span className="brand-name">隐去</span>
        <span className="active-filename" title={active?.name}>{active?.name}</span>
        {importing && <span className="import-status" role="status">{importing}</span>}
      </div>
      <div className="header-actions">
        <button className="button secondary" disabled={busy} onClick={() => filesInput.current?.click()}><Plus size={16} />导入图片</button>
        <button className="icon-button" title="选择文件夹" aria-label="选择文件夹" disabled={busy} onClick={() => folderInput.current?.click()}><FolderOpen size={18} /></button>
        <button className="button primary" disabled={busy || !confirmed.length} onClick={() => download()}><ArrowDownToLine size={16} />下载已确认图片{confirmed.length > 0 && <b>{confirmed.length}</b>}</button>
        {!!downloads.length && <button className="icon-button download-toggle" title={downloadProgress || '下载详情'} aria-label="下载详情" aria-expanded={showDownloads} onClick={() => setShowDownloads(!showDownloads)}>{downloadProgress ? <Loader2 size={17} className="spin" /> : <ArrowDownToLine size={17} />}{downloads.some(item => item.status === 'failed') && <i />}</button>}
        <button className="icon-button" title="设置" aria-label="设置" onClick={() => setShowSettings(true)}><Settings size={18} /></button>
        <button className="icon-button" aria-label={rulesCollapsed ? '展开规则与遮盖' : '收起规则与遮盖'} aria-pressed={!rulesCollapsed} onClick={() => setRulesCollapsed(!rulesCollapsed)}><PanelRight size={18} /></button>
      </div>
    </header>
    {error && <div className="banner error" role="alert"><AlertCircle size={17} /><span>{error}</span><button aria-label="关闭提示" onClick={() => setError('')}><X size={16} /></button></div>}
    {health?.settings_error && <div className="banner error" role="alert"><span>{health.settings_error}</span><button onClick={() => run(async () => { setRules(await json<RuleSet>('settings', { method: 'DELETE' })); setHealth(await json<Health>('health')); })}>重置规则</button></div>}
    {health && !health.models.ready && <div className="banner warning" role="alert">模型尚未准备，请运行“安装与准备.cmd”；仍可手动遮盖。</div>}
    {showDownloads && <section className="download-panel" aria-label="下载详情">
      <div className="popover-heading"><strong>{downloadProgress || `PNG 下载详情 · ${downloads.filter(item => item.status === 'submitted').length} / ${downloads.length} 张已提交浏览器`}</strong><button className="icon-button" aria-label="关闭下载详情" onClick={() => setShowDownloads(false)}><X size={16} /></button></div>
      <p>如浏览器提示，请允许此地址下载多个文件。</p>
      <ul>{downloads.map(item => {
        const current = images.find(image => image.id === item.id);
        const valid = current?.confirmed && current.revision === item.revision;
        return <li key={item.id+':'+item.revision}><span>{downloadName(item)}</span><span className={item.status === 'failed' ? 'download-error' : ''}>{!valid ? '内容已变更或图片已移除，请重新确认后下载' : item.status === 'failed' ? item.error : item.status === 'submitted' ? '已提交浏览器' : item.status === 'generating' ? item.progress || '正在生成…' : '等待生成'}</span><button className="text-button" disabled={busy || !valid} onClick={() => download(false, item)} aria-label={'下载 '+downloadName(item)}>{item.status === 'failed' ? '重试本张' : '下载本张'}</button></li>;
      })}</ul>
    </section>}
    <main className={'workspace' + (queueCollapsed || focusMode ? ' hide-queue' : '') + (rulesCollapsed || focusMode ? ' hide-rules' : '')}>
      <aside className="queue-panel" hidden={focusMode || queueCollapsed}>
        <div className="panel-heading"><h2>图片 <span>{images.length}</span></h2><span className="queue-summary">{confirmed.length} 已确认{pending > 0 ? ` · ${pending} ${health?.memory?.waiting_image_id ? '等待' : '处理中'}` : ''}</span></div>
        <div className="queue-list">{images.map((image, index) => <div key={image.id} className={'queue-item '+(image.id === activeId ? 'selected' : '')}>
          <button className="queue-select" onClick={() => setActiveId(image.id)} aria-label={'查看 '+image.name}>
            <span className="thumbnail">{urls[image.id] ? <img src={urls[image.id]} alt="" draggable={false} /> : <FileImage size={22} />}<small>{index+1}</small></span>
            <span className="queue-meta"><strong title={image.name}>{image.name}</strong><span className={image.confirmed ? 'status confirmed' : image.status === 'error' ? 'status issue' : 'status'}>
              {image.confirmed ? <CheckCircle2 size={12} /> : ['processing','queued'].includes(image.status) ? <Loader2 className="spin" size={12} /> : <Circle size={10} />}
              {image.confirmed ? '已确认' : image.status === 'waiting_memory' ? '等待释放内存' : image.status === 'processing' ? image.progress?.label || '正在识别' : image.status === 'queued' ? health?.memory?.waiting_image_id ? '队列已暂停' : '等待识别' : image.status === 'error' ? '需检查' : image.masks.length+' 处遮盖'}
            </span></span>
          </button><button className="queue-remove" aria-label={'移除 '+image.name} disabled={busy} onClick={() => removeImage(image)}><X size={13} /></button>
        </div>)}</div>
        <div className="queue-bottom"><button className="text-button" disabled={!images.length || busy} onClick={() => run(async () => { await request('images', { method: 'DELETE' }); history.current = {}; })}><Trash2 size={14} />清空当前批次</button></div>
      </aside>
      <section className="preview-panel">
        {active ? <>
          <Editor image={active} disabled={busy || !ready} selected={selected} onSelect={id => { setSelected(id); if (id) setRulesTab('masks'); }}
            onAdd={add} onChange={(mask, box) => edit(changeMask(active.edits, mask, box))} onRemove={() => remove()} onUndo={undo} onRedo={redo}
            canUndo={!!activeHistory?.past.length} canRedo={!!activeHistory?.future.length} actions={controls} focusTarget={focusTarget}
            focused={focusMode} onFocus={() => setFocusMode(!focusMode)} />
          <ProcessingStatus image={active} disconnected={disconnected} memory={health?.memory} />
          {reviewWarning && <div className="review-alert" role="status">
            <AlertCircle size={14} /><span>{reviewWarning}</span>
            {active.status === 'waiting_memory' && <details className="memory-help"><summary>释放内存</summary><p>当前可用内存 {((health?.memory?.available_bytes || 0)/1024**3).toFixed(1)} GiB。可先关闭其他占内存的程序；如有旧工具会话，请先导出图片，再关闭其启动窗口。只关闭浏览器标签页不会释放后台模型。当前编辑已保留。</p></details>}
            {retryable && <button className="text-button" disabled={busy} onClick={() => run(async () => { await json('images/'+active.id+'/retry', { method: 'POST' }); })}>重试处理</button>}
          </div>}
        </> : <div className="empty-canvas"><h1>拖入图片，开始脱敏</h1><div className="empty-actions"><button className="button primary" disabled={busy} onClick={() => filesInput.current?.click()}><ImagePlus size={17} />选择图片</button><button className="button secondary" disabled={busy} onClick={() => folderInput.current?.click()}><FolderOpen size={17} />选择文件夹</button></div></div>}
      </section>
      <RulesPanel hidden={focusMode || rulesCollapsed} rules={rules} busy={busy} image={active} selected={selected} onSelect={selectMask} onSave={saveRules} onEdit={edit} onRemove={remove} tab={rulesTab} onTab={setRulesTab} />
    </main>
    {showSettings && <SettingsDialog health={health} onClose={() => setShowSettings(false)} onUpdate={runtime => { refreshId.current++; setHealth(current => current ? {...current, runtime} : current); }} />}
    {toast && <div className="toast" role="status"><CheckCircle2 size={16} />{toast}</div>}
    {dragging && <div className="drop-overlay"><ImagePlus size={32} /><strong>松开鼠标，添加图片</strong></div>}
  </div>;
}

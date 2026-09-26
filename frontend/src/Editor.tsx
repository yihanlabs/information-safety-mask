import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Stage, Layer, Image as CanvasImage, Rect as CanvasRect, Transformer } from 'react-konva';
import Konva from 'konva';
import { ArrowLeftRight, Eye, Focus, Hand, Maximize, MousePointer2, Redo2, SquareDashed, Trash2, Undo2, ZoomIn, ZoomOut } from 'lucide-react';
import type { ImageRecord, Mask, Rect, Tool } from './types';
import { boundedRect, fromPoints } from './geometry';
import { request } from './api';

interface Props {
  image: ImageRecord; disabled: boolean; selected: string | null;
  onSelect: (id: string | null) => void; onAdd: (box: Rect) => void;
  onChange: (mask: Mask, box: Rect) => void; onRemove: () => void;
  onUndo: () => void; onRedo: () => void; canUndo: boolean; canRedo: boolean;
  actions: ReactNode; focused: boolean; onFocus: () => void;
  focusTarget: { id: string; nonce: number } | null;
}
type View = { scale: number; x: number; y: number; mode: 'width' | 'all' | 'free' };
type Size = { width: number; height: number };
export default function Editor(props: Props) {
  const { image, disabled, selected, onSelect, onAdd, onChange } = props;
  const holder = useRef<HTMLDivElement>(null);
  const stage = useRef<Konva.Stage>(null);
  const transformer = useRef<Konva.Transformer>(null);
  const [size, setSize] = useState<Size>({ width: 600, height: 500 });
  const [view, setView] = useState<View>({ scale: 1, x: 0, y: 0, mode: 'all' });
  const current = useRef(view);
  const views = useRef(new Map<string, { view: View; size: Size }>());
  const [bitmap, setBitmap] = useState<HTMLImageElement>();
  const [detail, setDetail] = useState<{ bitmap: HTMLImageElement; box: Rect }>();
  const [previewError, setPreviewError] = useState('');
  const [detailError, setDetailError] = useState(false);
  const [retry, setRetry] = useState(0);
  const [tool, setTool] = useState<Tool>('select');
  const [compare, setCompare] = useState(false);
  const [start, setStart] = useState<{ x: number; y: number } | null>(null);
  const [drawing, setDrawing] = useState<Rect | null>(null);
  const scrollDrag = useRef<{ pointer: number; y: number; offset: number } | null>(null);
  const { scale } = view;
  const editable = !disabled && !compare;
  const fitWidth = () => Math.max(Number.EPSILON, (size.width-32)/image.width);
  const fitAll = () => Math.max(Number.EPSILON, Math.min((size.width-32)/image.width, (size.height-32)/image.height, 1.4));
  function bounded(next: View): View {
    const axis = (position: number, pixels: number, viewport: number) => pixels <= viewport-32
      ? (viewport-pixels)/2 : Math.max(viewport-pixels-16, Math.min(16, position));
    return { ...next, x: axis(next.x, image.width*next.scale, size.width), y: axis(next.y, image.height*next.scale, size.height) };
  }
  function update(next: View) {
    const value = bounded(next);
    current.current = value;
    views.current.set(image.id, { view: value, size });
    setView(value);
  }
  function fit(mode: 'width' | 'all') {
    const next = mode === 'width' ? fitWidth() : fitAll();
    update({ scale: next, x: (size.width-image.width*next)/2, y: mode === 'width' ? 16 : (size.height-image.height*next)/2, mode });
  }
  useLayoutEffect(() => {
    const observer = new ResizeObserver(([entry]) => setSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
    if (holder.current) observer.observe(holder.current);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    const saved = views.current.get(image.id);
    if (!saved) { fit(image.height/image.width >= 3 ? 'width' : 'all'); return; }
    const previous = saved.view;
    const nextScale = previous.mode === 'width' ? fitWidth() : previous.mode === 'all' ? fitAll() : previous.scale;
    const center = { x: (saved.size.width/2-previous.x)/previous.scale, y: (saved.size.height/2-previous.y)/previous.scale };
    update({ scale: nextScale, x: size.width/2-center.x*nextScale, y: size.height/2-center.y*nextScale, mode: previous.mode });
  }, [size.width, size.height, image.id, image.width, image.height]);
  useEffect(() => {
    setBitmap(undefined); setDetail(undefined); setPreviewError('');
    if (!image.preview_ready) return;
    let active = true, objectUrl: string | undefined;
    const controller = new AbortController();
    request(`images/${image.id}/preview`, { signal: controller.signal }).then(r => r.blob()).then(blob => {
      if (!active) return;
      objectUrl = URL.createObjectURL(blob);
      const img = new window.Image();
      img.onload = () => { if (active) setBitmap(img); };
      img.onerror = () => { if (active) setPreviewError('预览载入失败'); };
      img.src = objectUrl;
    }).catch(e => { if (active && e.name !== 'AbortError') setPreviewError('预览载入失败'); });
    return () => { active = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [image.id, image.preview_ready, retry]);
  useEffect(() => {
    setDetail(undefined); setDetailError(false);
    if (!image.preview_ready || Math.max(image.width, image.height)*scale <= 2048) return;
    const controller = new AbortController();
    let active = true, objectUrl: string | undefined;
    const timer = setTimeout(() => {
      const x = Math.max(0, Math.floor(-view.x/scale)), y = Math.max(0, Math.floor(-view.y/scale));
      const right = Math.min(image.width, Math.ceil((size.width-view.x)/scale));
      const bottom = Math.min(image.height, Math.ceil((size.height-view.y)/scale));
      const width = right-x, height = bottom-y;
      if (width <= 0 || height <= 0) return;
      const ratio = Math.min(scale*Math.min(devicePixelRatio || 1, 2), 2048/width, 2048/height, 1);
      const query = new URLSearchParams({ x: String(x), y: String(y), width: String(width), height: String(height),
        output_width: String(Math.max(1, Math.ceil(width*ratio))), output_height: String(Math.max(1, Math.ceil(height*ratio))) });
      request(`images/${image.id}/region?${query}`, { signal: controller.signal }).then(r => r.blob()).then(blob => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        const img = new window.Image();
        img.onload = () => { if (active) setDetail({ bitmap: img, box: { x, y, width, height } }); };
        img.onerror = () => { if (active) setDetailError(true); };
        img.src = objectUrl;
      }).catch(e => { if (active && e.name !== 'AbortError') setDetailError(true); });
    }, 180);
    return () => { active = false; clearTimeout(timer); controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [image.id, image.preview_ready, image.width, image.height, scale, view.x, view.y, size.width, size.height, retry]);
  useEffect(() => { setCompare(false); setStart(null); setDrawing(null); }, [image.id]);
  useEffect(() => {
    const release = () => setCompare(false);
    window.addEventListener('pointerup', release); window.addEventListener('blur', release);
    return () => { window.removeEventListener('pointerup', release); window.removeEventListener('blur', release); };
  }, []);
  useEffect(() => {
    const node = selected ? stage.current?.findOne('#mask-'+selected) : null;
    transformer.current?.nodes(node && editable && tool === 'select' ? [node] : []);
  }, [selected, image.masks, editable, tool, compare]);
  useEffect(() => {
    const mask = image.masks.find(item => item.id === props.focusTarget?.id);
    if (!mask) return;
    const v = current.current, box = mask.box;
    const visible = box.x*v.scale+v.x >= 12 && box.y*v.scale+v.y >= 12 &&
      (box.x+box.width)*v.scale+v.x <= size.width-12 && (box.y+box.height)*v.scale+v.y <= size.height-12;
    if (visible && box.height*v.scale >= 10) return;
    const next = Math.max(v.scale, Math.min(1, (size.width-80)/box.width, (size.height-80)/box.height));
    update({ scale: next, x: size.width/2-(box.x+box.width/2)*next, y: size.height/2-(box.y+box.height/2)*next, mode: 'free' });
  }, [props.focusTarget?.nonce]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).closest('input, textarea, [contenteditable], dialog')) return;
      if (event.code === 'KeyV') setTool('select');
      if (event.code === 'KeyR') setTool('draw');
      if (event.code === 'KeyH') setTool('pan');
      if (event.code === 'KeyF' && !event.ctrlKey && !event.metaKey) { event.preventDefault(); props.onFocus(); }
      if (event.code === 'Escape' && props.focused) props.onFocus();
      if (['Home','End','PageDown','PageUp'].includes(event.code)) {
        event.preventDefault();
        const v = current.current;
        update({ ...v, y: event.code === 'Home' ? 16 : event.code === 'End' ? size.height-image.height*v.scale-16 : v.y+(event.code === 'PageDown' ? -1 : 1)*size.height*.9 });
      }
      if (!editable) return;
      if (event.code === 'Delete' || event.code === 'Backspace') { event.preventDefault(); props.onRemove(); }
      if ((event.ctrlKey || event.metaKey) && event.code === 'KeyZ') { event.preventDefault(); event.shiftKey ? props.onRedo() : props.onUndo(); }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  });
  function point() {
    const p = stage.current?.getPointerPosition(), v = current.current;
    return p ? { x: Math.min(image.width, Math.max(0, (p.x-v.x)/v.scale)), y: Math.min(image.height, Math.max(0, (p.y-v.y)/v.scale)) } : { x: 0, y: 0 };
  }
  function zoom(factor: number, anchor = { x: size.width/2, y: size.height/2 }) {
    const v = current.current;
    const next = Math.min(Math.max(8, fitWidth()), Math.max(fitAll()/4, v.scale*factor));
    update({ scale: next, x: anchor.x-(anchor.x-v.x)/v.scale*next, y: anchor.y-(anchor.y-v.y)/v.scale*next, mode: 'free' });
  }
  function finish() {
    if (drawing && drawing.width >= 2 && drawing.height >= 2 && editable) onAdd(drawing);
    setStart(null); setDrawing(null);
  }
  const scrollExtent = Math.max(0, image.height*scale-size.height+32);
  const trackHeight = Math.max(1, size.height-8);
  const thumbHeight = Math.min(trackHeight, Math.max(28, trackHeight*size.height/(image.height*scale+32)));
  const scrollOffset = Math.max(0, 16-view.y);
  const thumbTop = scrollExtent ? scrollOffset/scrollExtent*(trackHeight-thumbHeight) : 0;
  return <div className="editor">
    <div className="toolbar">
      <div className="toolbar-group">
        <button className={tool === 'select' ? 'tool active' : 'tool'} title="选择与调整 (V)" aria-label="选择与调整" onClick={() => setTool('select')}><MousePointer2 size={16} /></button>
        <button className={tool === 'draw' ? 'tool active' : 'tool'} title="手动补框 (R)" aria-label="手动补框" onClick={() => setTool('draw')}><SquareDashed size={17} /></button>
        <button className={tool === 'pan' ? 'tool active' : 'tool'} title="平移画布 (H)" aria-label="平移画布" onClick={() => setTool('pan')}><Hand size={16} /></button>
        <button className="tool" title="撤销 (Ctrl+Z)" aria-label="撤销" disabled={!props.canUndo || disabled} onClick={props.onUndo}><Undo2 size={16} /></button>
        <button className="tool" title="重做 (Ctrl+Shift+Z)" aria-label="重做" disabled={!props.canRedo || disabled} onClick={props.onRedo}><Redo2 size={16} /></button>
        <button className="tool" title="删除选中的遮盖框" aria-label="删除选中的遮盖框" disabled={!selected || disabled} onClick={props.onRemove}><Trash2 size={15} /></button>
      </div>
      <div className="toolbar-group zoom-controls">
        <button className="tool" aria-label="缩小" title="缩小" onClick={() => zoom(1/1.2)}><ZoomOut size={16} /></button><span className="zoom-label">{scale < .01 ? (scale*100).toFixed(2) : Math.round(scale*100)}%</span>
        <button className="tool" aria-label="放大" title="放大" onClick={() => zoom(1.2)}><ZoomIn size={16} /></button>
        <button className="tool" title="适应宽度" aria-label="适应宽度" onClick={() => fit('width')}><ArrowLeftRight size={16} /></button>
        <button className="tool" title="查看全图" aria-label="查看全图" onClick={() => fit('all')}><Maximize size={16} /></button>
        <button className={'tool compare-button'+(compare ? ' comparing' : '')} title="按住对照" aria-label="按住对照" onPointerDown={() => setCompare(true)} onPointerLeave={() => setCompare(false)} onKeyDown={e => { if (e.key === ' ') { e.preventDefault(); setCompare(true); } }} onKeyUp={() => setCompare(false)}><Eye size={16} /></button>
        <button className={'tool'+(props.focused ? ' active' : '')} title={props.focused ? '退出专注检查 (Esc)' : '专注检查 (F)'} aria-label={props.focused ? '退出专注检查' : '专注检查'} onClick={props.onFocus}><Focus size={16} /></button>
      </div>
      <div className="toolbar-spacer" /><div className="toolbar-actions">{props.actions}</div>
    </div>
    <div className={'canvas-area cursor-'+tool} ref={holder} id="image-viewport" data-testid="canvas" data-image-width={image.width} data-image-height={image.height}
      data-scale={scale} data-view-x={view.x} data-view-y={view.y}>
      <Stage ref={stage} width={size.width} height={size.height} scaleX={scale} scaleY={scale} x={view.x} y={view.y}
        draggable={tool === 'pan'} onDragEnd={e => { if (e.target === stage.current) update({ ...current.current, x: e.target.x(), y: e.target.y(), mode: 'free' }); }}
        onWheel={e => {
          e.evt.preventDefault();
          if (e.evt.ctrlKey || e.evt.metaKey) zoom(e.evt.deltaY < 0 ? 1.12 : 1/1.12, stage.current?.getPointerPosition() || undefined);
          else { const unit = e.evt.deltaMode === 1 ? 16 : e.evt.deltaMode === 2 ? size.height : 1;
            update({ ...current.current, mode: e.evt.shiftKey || e.evt.deltaX ? 'free' : current.current.mode, y: current.current.y-(e.evt.shiftKey ? 0 : e.evt.deltaY*unit), x: current.current.x-(e.evt.shiftKey ? e.evt.deltaY : e.evt.deltaX)*unit }); }
        }}
        onMouseDown={e => {
          if (!editable) return;
          if (tool === 'draw') { const p = point(); setStart(p); setDrawing({ ...p, width: 0, height: 0 }); onSelect(null); }
          else if (e.target === stage.current || e.target.name() === 'original') onSelect(null);
        }}
        onMouseMove={() => { if (start) setDrawing(fromPoints(start, point())); }} onMouseUp={finish}>
        <Layer>
          <CanvasRect width={image.width} height={image.height} fill="white" listening={false} />
          {bitmap && <CanvasImage name="original" image={bitmap} width={image.width} height={image.height} />}
          {detail && <CanvasImage image={detail.bitmap} {...detail.box} listening={false} />}
          {!compare && image.masks.map(mask => <CanvasRect key={mask.id} id={'mask-'+mask.id} {...mask.box} fill="#000000" stroke={selected === mask.id ? '#19a982' : undefined} strokeWidth={2/scale}
            draggable={tool === 'select' && editable} listening={tool === 'select' && editable}
            onClick={() => onSelect(mask.id)} onTap={() => onSelect(mask.id)} onDragStart={() => onSelect(mask.id)}
            onDragEnd={e => { e.cancelBubble = true; const box = boundedRect({ ...mask.box, x: e.target.x(), y: e.target.y() }, image.width, image.height); e.target.position({ x: box.x, y: box.y }); onChange(mask, box); }}
            onTransformEnd={e => { const node = e.target; const box = boundedRect({ x: node.x(), y: node.y(), width: Math.max(1, node.width()*node.scaleX()), height: Math.max(1, node.height()*node.scaleY()) }, image.width, image.height); node.scaleX(1); node.scaleY(1); node.position({ x: box.x, y: box.y }); onChange(mask, box); }} />)}
          {!compare && <Transformer ref={transformer} rotateEnabled={false} flipEnabled={false} keepRatio={false} borderStroke="#19a982" anchorStroke="#19a982" anchorFill="white" anchorSize={8} boundBoxFunc={(oldBox, newBox) => newBox.width < 3 || newBox.height < 3 ? oldBox : newBox} />}
          {drawing && <CanvasRect {...drawing} fill="rgba(0,0,0,.65)" stroke="#19a982" strokeWidth={2/scale} dash={[5/scale,4/scale]} listening={false} />}
        </Layer>
      </Stage>
      {!bitmap && <div className="canvas-loading">{previewError ? <button onClick={() => setRetry(retry+1)}>重新载入预览</button> : image.preview_ready ? '正在载入图片' : image.status === 'waiting_memory' ? '等待释放内存，可重试或移除此图片' : image.status === 'error' ? '图片尚未准备完成' : '正在准备图片'}</div>}
      {detailError && <button className="detail-retry" onClick={() => setRetry(retry+1)}>重试清晰预览</button>}
      {scrollExtent > 0 && <div className="image-scrollbar" role="scrollbar" aria-label="图片纵向位置" aria-orientation="vertical" aria-controls="image-viewport"
        aria-valuemin={0} aria-valuemax={Math.round(scrollExtent)} aria-valuenow={Math.round(scrollOffset)} tabIndex={0}
        onPointerDown={e => { e.preventDefault(); e.currentTarget.setPointerCapture(e.pointerId);
          const rect = e.currentTarget.getBoundingClientRect();
          const offset = e.target === e.currentTarget ? Math.max(0, Math.min(scrollExtent, (e.clientY-rect.top-thumbHeight/2)/(trackHeight-thumbHeight)*scrollExtent)) : scrollOffset;
          scrollDrag.current = { pointer: e.pointerId, y: e.clientY, offset };
          update({ ...current.current, y: 16-offset });
        }}
        onPointerMove={e => { if (scrollDrag.current?.pointer === e.pointerId) update({ ...current.current, y: 16-scrollDrag.current.offset-(e.clientY-scrollDrag.current.y)/(trackHeight-thumbHeight)*scrollExtent }); }}
        onKeyDown={e => { if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { e.preventDefault(); update({ ...current.current, y: current.current.y+(e.key === 'ArrowUp' ? 40 : -40) }); } }}
        onPointerUp={() => { scrollDrag.current = null; }} onPointerCancel={() => { scrollDrag.current = null; }}>
        <span style={{ height: thumbHeight, transform: `translateY(${thumbTop}px)` }} />
      </div>}
    </div>
  </div>;
}

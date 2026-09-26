import { useState } from 'react';
import { Check, Pencil, Plus, RotateCcw, X } from 'lucide-react';
import type { Category, EditState, ImageRecord, Mask, RuleSet } from './types';
const categories: { id: Category; label: string; detail: string }[] = [
  { id: 'person', label: '人名', detail: '姓名、联系人、收件人' },
  { id: 'identity', label: '证件号码', detail: '居民身份证、中国护照' },
  { id: 'address', label: '详细住址', detail: '街道、小区、楼栋与门牌' },
  { id: 'phone', label: '电话号码', detail: '手机、固定电话' },
];
interface Props {
  hidden: boolean;
  tab: 'rules' | 'masks'; onTab: (tab: 'rules' | 'masks') => void;
  rules: RuleSet; busy: boolean; image: ImageRecord | null; selected: string | null;
  onSelect: (id: string) => void; onSave: (rules: RuleSet) => Promise<boolean>;
  onEdit: (edits: EditState) => void; onRemove: (mask: Mask) => void;
}
export default function RulesPanel({ rules, busy, image, selected, onSelect, onSave, onEdit, onRemove, tab, onTab, hidden }: Props) {
  const [text, setText] = useState('');
  const [editing, setEditing] = useState<string | null>(null);
  const [value, setValue] = useState('');
  const ready = image && !['queued', 'processing'].includes(image.status);
  async function addFields() {
    const seen = new Set(rules.custom_fields.map(item => item.value.normalize('NFKC').replace(/\s/g, '')));
    const added = text.split(/\r?\n/).map(item => item.trim()).filter(item => {
      const key = item.normalize('NFKC').replace(/\s/g, '');
      if (!key || seen.has(key)) return false;
      seen.add(key); return true;
    }).map(value => ({ id: crypto.randomUUID(), value, enabled: true }));
    if (added.length && await onSave({ ...rules, custom_fields: [...rules.custom_fields, ...added] })) setText('');
  }
  return <aside className="rules-panel" hidden={hidden}>
    <div className="rules-tabs" role="tablist" aria-label="规则与遮盖">
      <button role="tab" aria-selected={tab === 'rules'} onClick={() => onTab('rules')}>批次规则</button>
      <button role="tab" aria-selected={tab === 'masks'} onClick={() => onTab('masks')}>当前遮盖 {image?.masks.length || 0}</button>
    </div>
    <div className="rules-scroll">
      <div hidden={tab !== 'rules'} role="tabpanel" aria-label="批次规则">
      <section className="rule-section">
        <div className="section-title"><h3>敏感类别</h3></div>
        <div className="category-list">{categories.map(({ id, label, detail }) =>
          <label key={id} title={detail} className={'category-option ' + (rules.categories.includes(id) ? 'enabled' : '')}>
            <input type="checkbox" aria-label={label} checked={rules.categories.includes(id)} disabled={busy}
              onChange={e => onSave({ ...rules, categories: e.target.checked ? [...rules.categories, id] : rules.categories.filter(item => item !== id) })} />
            <span><strong>{label}</strong></span><span className="checkbox-mark"><Check size={12} /></span>
          </label>)}</div>
      </section>
      <section className="rule-section custom-section">
        <div className="section-title"><h3>自定义敏感内容</h3><span>{rules.custom_fields.filter(item => item.enabled).length} 项启用</span></div>

        <textarea aria-label="自定义敏感内容" placeholder={'需要遮盖的内容，每行一项\n例如：张三'} value={text} maxLength={5000} disabled={busy} onChange={e => setText(e.target.value)} rows={3} />
        <button className="add-fields" disabled={busy || !text.trim()} onClick={addFields}><Plus size={15} />添加到字段列表</button>
        <div className="custom-list">{rules.custom_fields.map(field =>
          <div className="custom-row" key={field.id}>{editing === field.id ? <>
            <input className="field-edit" aria-label="编辑字段内容" value={value} maxLength={500} onChange={e => setValue(e.target.value)} />
            <button className="icon-button" aria-label="保存字段" disabled={busy || !value.trim()} onClick={async () => {
              if (await onSave({ ...rules, custom_fields: rules.custom_fields.map(item => item.id === field.id ? { ...item, value: value.trim() } : item) })) setEditing(null);
            }}><Check size={15} /></button>
            <button className="icon-button" aria-label="取消编辑" onClick={() => setEditing(null)}><X size={14} /></button>
          </> : <>
            <label><input type="checkbox" checked={field.enabled} disabled={busy} onChange={e => onSave({ ...rules, custom_fields: rules.custom_fields.map(item => item.id === field.id ? { ...item, enabled: e.target.checked } : item) })} /><span title={field.value}>{field.value}</span></label>
            <button className="mini-action" aria-label={'编辑 ' + field.value} disabled={busy} onClick={() => { setEditing(field.id); setValue(field.value); }}><Pencil size={13} /></button>
            <button className="mini-action" aria-label={'删除字段 ' + field.value} disabled={busy} onClick={() => onSave({ ...rules, custom_fields: rules.custom_fields.filter(item => item.id !== field.id) })}><X size={14} /></button>
          </>}</div>)}</div>
        {!rules.categories.length && !rules.custom_fields.some(field => field.enabled) && <p className="manual-mode"><Pencil size={13} />当前为手动遮盖模式</p>}
        {rules.custom_fields.length > 0 && <button className="text-button" disabled={busy} onClick={() => onSave({ ...rules, custom_fields: [] })}>清空字段</button>}
      </section>
      </div><section hidden={tab !== 'masks'} role="tabpanel" aria-label="当前遮盖" className="rule-section masks-section">

        {image?.masks.length ? <div className="mask-list">{image.masks.map((mask, index) =>
          <div className={'mask-item ' + (selected === mask.id ? 'chosen' : '')} key={mask.id}>
            <button className="mask-info" onClick={() => onSelect(mask.id)}><span className="mask-number">{String(index + 1).padStart(2, '0')}</span>
              <span><strong>{mask.reasons[0]?.split(' · ')[0] || '遮盖区域'}</strong>{selected === mask.id && <small>{mask.reasons.join('；')}</small>}{mask.fallback && <em>整行遮盖，可调整</em>}</span>
            </button><button className="mini-action" disabled={busy || !ready} aria-label={'取消遮盖 ' + (index + 1)} onClick={() => onRemove(mask)}><X size={14} /></button>
          </div>)}</div> : <p className="mask-empty">{image ? ready ? '暂无遮盖区域，可使用“补框”手动添加。' : '识别完成后在这里查看命中原因。' : '导入图片后，在这里查看和调整遮盖。'}</p>}
        {image && (image.excluded_count > 0 || Object.keys(image.edits.overrides).length > 0) &&
          <button className="text-button restore" disabled={busy || !ready} onClick={() => onEdit({ ...structuredClone(image.edits), excluded: [], overrides: {} })}><RotateCcw size={13} />恢复自动遮盖{image.excluded_count > 0 && <span>已取消 {image.excluded_count} 处</span>}</button>}
      </section>
    </div>

  </aside>;
}

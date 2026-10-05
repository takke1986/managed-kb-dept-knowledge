// 書き起こしの確認と修正。左に書き起こし（ページ・スライド・シートごと）、右に元ファイルのそのページを並べる。
// 直すと、同じ中身の文書すべての書き起こしが変わり、ナレッジも入れ直される。書き起こし直しでも消えない。
import { useCallback, useEffect, useState } from 'react';
import { api } from './api';

interface Page { index: number; label: string; text: string }
interface Transcript {
  key: string;
  fileName: string;
  pageKind: 'page' | 'slide' | '';
  pages: Page[];
  edited: boolean;
  editedBy: string;
  editedAt: string;
  version: number;
  url: string;
}

export function TranscriptViewer({ fileKey, page, onClose }: { fileKey: string; page?: number; onClose: () => void }) {
  const [t, setT] = useState<Transcript>();
  const [index, setIndex] = useState(0);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [message, setMessage] = useState('');

  const load = useCallback(async (keepIndex?: number) => {
    try {
      const r = await api<Transcript>('GET', `/api/files/text?key=${encodeURIComponent(fileKey)}`);
      setT(r);
      const wanted = keepIndex ?? (page ? r.pages.findIndex((p) => p.label === String(page)) : 0);
      setIndex(Math.max(0, wanted));
    } catch (e) {
      setMessage((e as Error).message);
    }
  }, [fileKey, page]);
  useEffect(() => { load(); }, [load]);

  const current = t?.pages[index];
  const label = (p: Page) => (t?.pageKind === 'slide' ? `スライド${p.label}`
    : t?.pageKind === 'page' && /^\d+$/.test(p.label) ? `p.${p.label}` : p.label);
  const original = t && current
    ? (t.pageKind === 'page' && /^\d+$/.test(current.label) ? `${t.url}#page=${current.label}` : t.url) : '';
  const isImage = t && /\.(png|jpe?g|gif|webp)$/i.test(t.fileName);

  const save = async () => {
    if (!t || !current) return;
    try {
      await api('PUT', '/api/files/text', { key: t.key, index: current.index, text: draft, version: t.version });
      setMessage('保存しました。ナレッジへの反映には1〜2分かかります');
      setEditing(false);
      load(index);
    } catch (e) {
      setMessage((e as Error).message);
    }
  };

  return (
    <div className="modal" role="dialog" aria-label="書き起こし">
      <div className="modal-body">
        <div className="row">
          <h2 style={{ flex: 1, margin: 0 }}>書き起こし: {t?.fileName ?? ''}</h2>
          <button onClick={onClose}>閉じる</button>
        </div>
        {t?.edited && <p className="muted">人が直した書き起こしです（{t.editedBy}、{new Date(t.editedAt).toLocaleString('ja-JP')}）</p>}
        {message && <p className="muted">{message}</p>}
        {t && (
          <>
            <div className="row">
              <select value={index} onChange={(e) => { setIndex(Number(e.target.value)); setEditing(false); }} aria-label="ページ">
                {t.pages.map((p) => <option key={p.index} value={p.index}>{label(p)}</option>)}
              </select>
              {!editing
                ? <button onClick={() => { setDraft(current?.text ?? ''); setEditing(true); setMessage(''); }}>このページを直す</button>
                : <>
                  <button className="primary" onClick={save}>保存</button>
                  <button onClick={() => setEditing(false)}>やめる</button>
                </>}
            </div>
            <div className="transcript">
              {editing
                ? <textarea value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="書き起こしを直す" />
                : <pre>{current?.text}</pre>}
              <div className="original">
                {isImage ? <img src={t.url} alt={t.fileName} />
                  : t.pageKind === 'page' ? <iframe key={original} src={original} title="元ファイル" />
                    : <p className="muted">この形式は並べて表示できません。<a href={t.url} target="_blank" rel="noreferrer">元ファイルを開く</a></p>}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

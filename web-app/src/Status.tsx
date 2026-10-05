// 置いたファイルがナレッジに入ったか（書き起こし中・取り込み済み・失敗）とタグを見せる。
// Storage Browser には出ない情報なので、別の画面に一覧にする。Storage Browser で開いているフォルダに合わせて絞る（外せる）。
//
// タグは2種類。フォルダ名のタグ（置いた場所で決まる。変えるにはフォルダを移す）と、
// 追加のタグ（部署のタグの一覧から選ぶ）。ファイルにチェックを入れると上にタグのボタンが並び、
// 押すと選んだファイル全部に付ける（全部に付いているものは外す）。
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, openOriginal, type FileStatus, type Me } from './api';
import { TranscriptViewer } from './Transcript';

const LABEL = {
  converted: '取り込み済み', converting: '書き起こし中', failed: '失敗', stalled: '止まっている可能性',
  skipped: '対象外', blocked: 'ブロック（マルウェアの疑い）',
} as const;

export interface Browsing {
  department: string;
  path: string; // 部署のフォルダより下（例: 確認用/Office/）
}

export function Status({ me, browsing }: { me: Me; browsing?: Browsing }) {
  const [dept, setDept] = useState(me.departments[0]?.id ?? '');
  const [files, setFiles] = useState<FileStatus[]>([]);
  const [error, setError] = useState('');
  const [text, setText] = useState('');
  const [tagFilter, setTagFilter] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [viewing, setViewing] = useState('');

  // Storage Browser で開いているフォルダに合わせる
  useEffect(() => {
    if (browsing?.department && me.departments.some((d) => d.id === browsing.department)) setDept(browsing.department);
  }, [browsing?.department, me.departments]);
  // ファイル置き場で開いているフォルダに絞る。外したら、次にフォルダを移るまで全体を出す
  const [ignored, setIgnored] = useState<Browsing>();
  const folder = browsing?.department === dept && browsing !== ignored ? browsing.path : '';
  const department = me.departments.find((d) => d.id === dept);
  const choices = department?.tags ?? [];
  const defs = useMemo(() => new Map((department?.tagDefs ?? []).map((t) => [t.name, t])), [department]);

  const load = useCallback(async () => {
    if (!dept) return;
    try {
      const r = await api<{ files: FileStatus[] }>('GET', `/api/files?department=${encodeURIComponent(dept)}`);
      setFiles(r.files);
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, [dept]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 15000); // 書き起こしは数秒〜数分で終わる
    return () => clearInterval(timer);
  }, [load]);
  useEffect(() => setSelected(new Set()), [dept, folder]);

  const shown = useMemo(() => files.filter((f) =>
    (!folder || f.key.startsWith(`${dept}/${folder}`))
    && (!text || f.fileName.includes(text) || f.folder.includes(text))
    && (!tagFilter || (tagFilter === '（タグなし）' ? f.tags.length === 0 : f.tags.includes(tagFilter)))),
  [files, folder, dept, text, tagFilter]);
  const allTags = useMemo(() => [...new Set(files.flatMap((f) => f.tags))].sort(), [files]);
  const selectable = shown.filter((f) => f.status === 'converted');
  const allSelected = selectable.length > 0 && selectable.every((f) => selected.has(f.key));

  const toggle = (key: string) => setSelected((s) => {
    const next = new Set(s);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(selectable.map((f) => f.key)));

  // 選んだファイルそれぞれに、そのタグが付いているか（全部・一部・なし）
  const chosen = files.filter((f) => selected.has(f.key));
  const chips = useMemo(() => {
    const onSelected = chosen.flatMap((f) => f.extraTags);
    return [...new Set([...choices, ...onSelected])].map((name) => {
      const n = chosen.filter((f) => f.extraTags.includes(name)).length;
      return { name, n, all: n === chosen.length && n > 0, usable: choices.includes(name) };
    });
  }, [chosen, choices]);

  const apply = async (tag: string, mode: 'add' | 'remove') => {
    if (!chosen.length || busy) return;
    setBusy(true);
    try {
      const r = await api<{ updated: string[]; skipped: string[] }>('PUT', '/api/files/tags',
        { keys: chosen.map((f) => f.key), [mode]: [tag] });
      setMessage(`${r.updated.length}件${mode === 'add' ? 'に' : 'から'}「${tag}」を${mode === 'add' ? '付けました' : '外しました'}。`
        + 'ナレッジへの反映には1〜2分かかります'
        + (r.skipped.length ? `（まだナレッジに入っていない ${r.skipped.length}件は飛ばしました）` : ''));
      setError('');
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <div className="row">
        <h2 style={{ flex: 1, margin: 0 }}>
          ナレッジへの取り込み状況
          {folder && <span className="muted">（ファイル置き場で開いている {folder.replace(/\/$/, '')} 以下）
            <button className="link" onClick={() => setIgnored(browsing)}>すべて表示</button></span>}
        </h2>
        {me.departments.length > 1 && (
          <select value={dept} onChange={(e) => setDept(e.target.value)} aria-label="部署">
            {me.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        )}
        <button onClick={load}>更新</button>
      </div>

      <div className="row filters">
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="ファイル名・フォルダで絞り込む"
          aria-label="ファイル名・フォルダで絞り込む" style={{ flex: 1, minWidth: 160 }} />
        <select value={tagFilter} onChange={(e) => setTagFilter(e.target.value)} aria-label="タグで絞り込む">
          <option value="">すべてのタグ</option>
          <option value="（タグなし）">（タグなし）</option>
          {allTags.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>

      <div className="tagbar" aria-live="polite">
        <div className="row">
          <label><input type="checkbox" checked={allSelected} onChange={toggleAll} disabled={!selectable.length} /> 表示中をすべて選ぶ</label>
          {chosen.length > 0 && (
            <>
              <strong>{chosen.length}件を選択中</strong>
              <button className="link" onClick={() => setSelected(new Set())}>選択を解除</button>
            </>
          )}
        </div>
        {chosen.length === 0 ? (
          <p className="muted" style={{ margin: '4px 0 0' }}>
            タグを付ける・外すには、ファイルの左のチェックを入れます（取り込み済みのファイルだけ選べます）。
          </p>
        ) : (
          <>
            <p className="muted" style={{ margin: '4px 0' }}>
              ＋ のタグを押すと、選んだファイル全部に付けます。✓ のタグ（全部に付いている）を押すと外します。
            </p>
            <div className="chips" role="group" aria-label="選んだファイルに付けるタグ">
              {chips.length === 0 && <span className="muted">付けられるタグがありません。管理者に「タグの管理」で登録してもらってください</span>}
              {chips.map((c) => {
                const def = defs.get(c.name);
                const color = `c-${def?.color ?? 'gray'}`;
                const how = c.all ? '選んだファイル全部に付いています。押すと外します'
                  : c.n > 0 ? `選んだうち${c.n}件に付いています。押すと残りにも付けます。× で外します`
                    : '押すと、選んだファイル全部に付けます';
                return (
                  <span key={c.name} className="chip-wrap">
                    <button className={`chip ${color} ${c.all ? 'on' : c.n ? 'some' : ''}`}
                      aria-pressed={c.all ? true : c.n ? 'mixed' : false}
                      disabled={busy || (!c.all && !c.usable)} onClick={() => apply(c.name, c.all ? 'remove' : 'add')}
                      title={[def?.description, c.usable || c.all ? how : 'アーカイブしたタグなので、外すことだけできます'].filter(Boolean).join('\n')}>
                      <span className="mark">{c.all ? '✓' : c.n ? '−' : '+'}</span><span>{c.name}</span>
                      {c.n > 0 && !c.all && <span className="count">{c.n}/{chosen.length}</span>}
                    </button>
                    {c.n > 0 && !c.all && (
                      <button className={`chip-x ${color}`} disabled={busy} onClick={() => apply(c.name, 'remove')}
                        aria-label={`${c.name} を外す`} title={`付いている${c.n}件から「${c.name}」を外します`}>×</button>
                    )}
                  </span>
                );
              })}
            </div>
          </>
        )}
        {message && <p className="muted" role="status" style={{ margin: '6px 0 0' }}>{message}</p>}
        {error && <p className="err" role="alert" style={{ margin: '6px 0 0' }}>{error}</p>}
      </div>

      <ul className="files">
        {shown.length === 0 && <li className="muted">当てはまるファイルがありません</li>}
        {shown.map((f) => (
          <li key={f.key} className="file-row">
            <input type="checkbox" checked={selected.has(f.key)} onChange={() => toggle(f.key)}
              disabled={f.status !== 'converted'} aria-label={`${f.fileName} を選ぶ`}
              title={f.status === 'converted' ? undefined : 'ナレッジに入ってから選べます'} />
            <div>
              <a href="#" onClick={(e) => { e.preventDefault(); openOriginal(f.key); }}>{f.fileName}</a>
              <br />
              <span className={`badge st-${f.status}`}>{LABEL[f.status]}</span>
              {f.folderTags.map((t) => <span key={`f-${t}`} className="badge tag folder" title="フォルダ名のタグ">📁{t}</span>)}
              {f.extraTags.map((t) => (
                <span key={`e-${t}`} className={`badge tag c-${defs.get(t)?.color ?? 'gray'}`}
                  title={[defs.get(t)?.description, defs.get(t)?.archived ? '（アーカイブ済み）' : ''].filter(Boolean).join(' ') || undefined}>
                  {t}
                </span>
              ))}
              <span className="muted">{new Date(f.uploadedAt).toLocaleString('ja-JP')}</span>
              {f.reason && <div className="muted">{f.reason}</div>}
              {f.status === 'converted' && <button className="link" onClick={() => setViewing(f.key)}>書き起こしを見る・直す</button>}
            </div>
          </li>
        ))}
      </ul>
      {viewing && <TranscriptViewer fileKey={viewing} onClose={() => setViewing('')} />}
      <p className="muted">📁 はフォルダ名のタグで、置いた場所で決まります（変えるにはファイル置き場でフォルダを移す）。ほかのタグは、チェックを入れたファイルに上のボタンで付ける・外す。</p>
    </section>
  );
}

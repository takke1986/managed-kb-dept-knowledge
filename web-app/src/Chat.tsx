import { useRef, useState } from 'react';
import { api, openOriginal, type Department, type Source } from './api';
import { TranscriptViewer } from './Transcript';

interface Message {
  role: 'user' | 'assistant';
  text: string;
  sources?: Source[];
  pending?: boolean;
}

export function Chat({ departments }: { departments: Department[] }) {
  const [messages, setMessages] = useState<Message[]>([]);
  // 兼務の人は検索する部署を選べる（既定はすべて）
  const [scope, setScope] = useState<string[]>(departments.map((d) => d.id));
  const toggleScope = (id: string) =>
    setScope((s) => (s.includes(id) ? (s.length > 1 ? s.filter((x) => x !== id) : s) : [...s, id]));
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [viewing, setViewing] = useState<{ key: string; page?: number }>();
  const logRef = useRef<HTMLDivElement>(null);

  const scroll = () => setTimeout(() => logRef.current?.scrollTo(0, logRef.current.scrollHeight), 0);

  async function send() {
    const q = input.trim();
    if (!q || busy) return;
    const history = messages.filter((m) => !m.pending).map(({ role, text }) => ({ role, text }));
    setInput('');
    setBusy(true);
    setMessages((m) => [...m, { role: 'user', text: q }, { role: 'assistant', text: '検索しています…', pending: true }]);
    scroll();
    try {
      // 受け付けたら、書けた分から表示する（1秒弱ごとに問い合わせる）
      const { jobId } = await api<{ jobId: string }>('POST', '/api/chat', { message: q, history, departments: scope });
      for (;;) {
        await new Promise((r) => setTimeout(r, 800));
        const j = await api<{ status: string; text: string; answer: string; sources: Source[] }>(
          'GET', `/api/chat?id=${encodeURIComponent(jobId)}`);
        if (j.status === 'running') {
          if (j.text) setMessages((m) => [...m.slice(0, -1), { role: 'assistant', text: j.text, pending: true }]);
          scroll();
          continue;
        }
        setMessages((m) => [...m.slice(0, -1), { role: 'assistant', text: j.answer || j.text, sources: j.sources }]);
        break;
      }
    } catch (e) {
      setMessages((m) => [...m.slice(0, -1), { role: 'assistant', text: (e as Error).message }]);
    } finally {
      setBusy(false);
      scroll();
    }
  }

  return (
    <section className="panel chat">
      <h2>質問する</h2>
      <p className="muted">自分の部署の文書だけから答えます。</p>
      {departments.length > 1 && (
        <div className="row scope" role="group" aria-label="検索する部署">
          <span className="muted">検索する部署:</span>
          {departments.map((d) => (
            <label key={d.id}><input type="checkbox" checked={scope.includes(d.id)} onChange={() => toggleScope(d.id)} /> {d.name}</label>
          ))}
        </div>
      )}
      <div className="log" ref={logRef}>
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role === 'user' ? 'user' : 'bot'}`}>
            {m.text}
            {m.sources && m.sources.length > 0 && (
              <div className="sources">
                <strong>参照した元ファイル</strong>
                {m.sources.map((s) => (
                  <div key={s.key}>
                    <a href="#" onClick={(e) => { e.preventDefault(); openOriginal(s.key, s.pageKind === 'page' ? s.pages[0] : undefined); }}>{s.fileName}</a>{' '}
                    {s.pages.map((p) => (
                      <a key={p} href="#" className="page" onClick={(e) => { e.preventDefault(); openOriginal(s.key, s.pageKind === 'page' ? p : undefined); }}>
                        {s.pageKind === 'slide' ? `スライド${p}` : `p.${p}`}
                      </a>
                    ))}{' '}
                    <span className="muted">
                      {s.department}{s.folder ? ` / ${s.folder}` : ''}{s.tags.length ? ` / ${s.tags.join('・')}` : ''}
                    </span>{' '}
                    <a href="#" className="page" onClick={(e) => { e.preventDefault(); setViewing({ key: s.key, page: s.pages[0] }); }}>書き起こし</a>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      {viewing && <TranscriptViewer fileKey={viewing.key} page={viewing.page} onClose={() => setViewing(undefined)} />}
      <form className="row" onSubmit={(e) => { e.preventDefault(); send(); }}>
        <textarea
          rows={2}
          value={input}
          placeholder="例: 反社会的勢力の条項がある契約はどれ？"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send(); }}
        />
        <button className="primary" type="submit" disabled={busy}>送信</button>
      </form>
    </section>
  );
}

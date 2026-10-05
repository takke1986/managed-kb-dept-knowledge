// 自分へのお知らせ（置いたファイルの書き起こしの失敗・対象外・ブロック）。画面の上に件数を出す。
import { useCallback, useEffect, useState } from 'react';
import { api } from './api';

interface Item {
  sk: string;
  key: string;
  fileName: string;
  status: string;
  title: string;
  reason: string;
  read: boolean;
  createdAt: string;
}

export function Notifications() {
  const [items, setItems] = useState<Item[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api<{ items: Item[]; unread: number }>('GET', '/api/notifications');
      setItems(r.items);
      setUnread(r.unread);
    } catch { /* お知らせが読めなくても画面は使える */ }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 60000);
    return () => clearInterval(timer);
  }, [load]);

  const toggle = async () => {
    setOpen((o) => !o);
    const ids = items.filter((i) => !i.read).map((i) => i.sk);
    if (!open && ids.length) {
      await api('POST', '/api/notifications/read', { ids }).catch(() => undefined);
      setUnread(0);
    }
  };

  return (
    <div className="notice">
      <button onClick={toggle} aria-expanded={open}>
        お知らせ{unread > 0 && <span className="notice-count">{unread}</span>}
      </button>
      {open && (
        <div className="notice-list" role="dialog" aria-label="お知らせ">
          {items.length === 0 && <p className="muted">お知らせはありません</p>}
          {items.map((i) => (
            <div key={i.sk} className={`notice-item st-${i.status}`}>
              <strong>{i.fileName}</strong>：{i.title}
              <div className="muted">{i.reason}</div>
              <div className="muted">{new Date(i.createdAt).toLocaleString('ja-JP')}（{i.key.split('/').slice(0, -1).join(' / ')}）</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

'use client';

import { useEffect, useMemo, useState } from 'react';

const STATUS_ORDER = ['interviewing', 'screening', 'applied', 'needs_human', 'rejected'];

function Links({ r }) {
  const k = r.links;
  return (
    <span className="links">
      {r.url && <a href={r.url} target="_blank" rel="noopener">posting ↗</a>}
      {k.jd && <a href={`/jd/${r.job_id}`} target="_blank" rel="noopener">JD</a>}
      {k.resume && <a href={`/files/${r.job_id}/resume`} target="_blank" rel="noopener">resume</a>}
      {k.cover && <a href={`/files/${r.job_id}/cover`} target="_blank" rel="noopener">cover</a>}
      {k.answers > 0 && (
        <a href={`/answers/${r.job_id}`} target="_blank" rel="noopener">answers ({k.answers})</a>
      )}
      {(k.post_shot || k.pre_shot) && (
        <a href={`/files/${r.job_id}/${k.post_shot ? 'post_shot' : 'pre_shot'}`} target="_blank" rel="noopener">
          shot
        </a>
      )}
    </span>
  );
}

export default function Page() {
  const [rows, setRows] = useState(null);
  const [q, setQ] = useState('');
  const [statusFilter, setStatusFilter] = useState(null);

  useEffect(() => {
    fetch('/api/applications')
      .then((r) => r.json())
      .then(setRows)
      .catch(() => setRows([]));
  }, []);

  const counts = useMemo(() => {
    const c = {};
    (rows || []).forEach((r) => { c[r.status] = (c[r.status] || 0) + 1; });
    return c;
  }, [rows]);

  const visible = useMemo(() => {
    let v = rows || [];
    if (statusFilter) v = v.filter((r) => r.status === statusFilter);
    if (q.trim()) {
      const needle = q.trim().toLowerCase();
      v = v.filter((r) =>
        `${r.company} ${r.title} ${r.location} ${r.status}`.toLowerCase().includes(needle)
      );
    }
    return v;
  }, [rows, q, statusFilter]);

  if (rows === null) return <div className="wrap"><p className="mut">loading…</p></div>;

  const statuses = [...STATUS_ORDER.filter((s) => counts[s]),
    ...Object.keys(counts).filter((s) => !STATUS_ORDER.includes(s))];

  return (
    <div className="wrap">
      <header className="top">
        <h1>honestapply — applications</h1>
        <span className="sub">
          every application · JD, résumé, cover letter, submitted answers &amp; screenshots stored locally
        </span>
      </header>

      <div className="cards">
        <div
          className={`card ${statusFilter === null ? 'active' : ''}`}
          onClick={() => setStatusFilter(null)}
        >
          <div className="n">{rows.length}</div>
          <div className="l">total</div>
        </div>
        {statuses.map((s) => (
          <div
            key={s}
            className={`card ${s} ${statusFilter === s ? 'active' : ''}`}
            onClick={() => setStatusFilter(statusFilter === s ? null : s)}
          >
            <div className="n">{counts[s]}</div>
            <div className="l">{s.replace('_', ' ')}</div>
          </div>
        ))}
      </div>

      <div className="controls">
        <input
          placeholder="filter company, role, location…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      <table>
        <thead>
          <tr>
            <th>Company</th>
            <th>Role</th>
            <th>Location</th>
            <th>Status</th>
            <th>Score</th>
            <th>Applied</th>
            <th>Everything stored locally</th>
            <th>Next / note</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((r) => (
            <tr key={r.job_id}>
              <td><b>{r.company}</b></td>
              <td>{r.title}</td>
              <td className="mut">{r.location}</td>
              <td><span className={`badge ${r.status}`}>{r.status.replace('_', ' ')}</span></td>
              <td className="score">{r.score ?? ''}</td>
              <td className="mut">{r.applied_at}</td>
              <td><Links r={r} /></td>
              <td className="note">{r.next_action || r.confirmation.slice(0, 90)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {visible.length === 0 && <p className="mut">nothing matches.</p>}
    </div>
  );
}

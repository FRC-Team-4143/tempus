/* Shared rendering + refresh logic for the kiosk display boards.
 *
 * Used by three pages: the combined auto-swapping display (/kiosk) and the two
 * pinned views (/kiosk/student, /kiosk/mentor). Boards are bound to a root
 * element and address their parts through data- attributes rather than global
 * ids, so both can coexist in one document on the combined page.
 *
 * Each board exposes refresh() and isEmpty(); the combined page's swap
 * controller is built on exactly those two, triggered by initBadgeScanner's
 * optional onResult callback (this browser's own scan) rather than by the SSE
 * events also handled below, which are data-refresh only.
 */
window.Kiosk = (function () {
  'use strict';

  function escHtml(str) {
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(str == null ? '' : String(str)));
    return d.innerHTML;
  }

  // ── Clock ────────────────────────────────────────────────────────────────
  function startClock() {
    const el = document.getElementById('clock');
    if (!el) return;
    const tick = () => {
      el.textContent = new Date().toLocaleTimeString([], {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
    };
    tick();
    setInterval(tick, 1000);
  }

  // ── Auto-scroll ──────────────────────────────────────────────────────────
  const SCROLL_PX_PER_SEC = 45;
  const PAUSE_TOP_MS = 2500;
  const PAUSE_BOTTOM_MS = 1500;
  const _scrollState = new WeakMap();

  function startAutoScroll(el) {
    if (!el) return;
    const prev = _scrollState.get(el);
    if (prev) cancelAnimationFrame(prev);
    el.scrollTop = 0;

    let lastTs = null;
    let phase = 'pause-top';   // pause-top | down | pause-bottom | up
    let phaseStart = null;

    function tick(ts) {
      // Content fits — nothing to scroll, but keep the loop alive so a later
      // refresh that overflows starts moving without a re-init.
      if (el.scrollHeight <= el.clientHeight + 2) {
        _scrollState.set(el, requestAnimationFrame(tick));
        return;
      }
      if (phase === 'pause-top') {
        if (phaseStart === null) phaseStart = ts;
        if (ts - phaseStart >= PAUSE_TOP_MS) { phase = 'down'; phaseStart = null; lastTs = ts; }
      } else if (phase === 'down') {
        el.scrollTop += SCROLL_PX_PER_SEC * ((ts - lastTs) / 1000);
        lastTs = ts;
        if (el.scrollTop + el.clientHeight >= el.scrollHeight - 2) {
          phase = 'pause-bottom'; phaseStart = null;
        }
      } else if (phase === 'pause-bottom') {
        if (phaseStart === null) phaseStart = ts;
        if (ts - phaseStart >= PAUSE_BOTTOM_MS) { phase = 'up'; phaseStart = null; lastTs = ts; }
      } else if (phase === 'up') {
        el.scrollTop -= SCROLL_PX_PER_SEC * ((ts - lastTs) / 1000);
        lastTs = ts;
        if (el.scrollTop <= 2) { el.scrollTop = 0; phase = 'pause-top'; phaseStart = null; }
      }
      _scrollState.set(el, requestAnimationFrame(tick));
    }

    _scrollState.set(el, requestAnimationFrame(tick));
  }

  function initScrollers(root) {
    (root || document).querySelectorAll('.scroll-list').forEach(startAutoScroll);
  }

  // ── Leaderboard rendering (identical shape on both boards) ───────────────
  const STAT_SECTIONS = [
    { key: 'alltime',         icon: 'bi-trophy-fill',    label: 'All-Time Hours',  color: '#e05540' },
    { key: 'week',            icon: 'bi-calendar-week',  label: 'This Week',       color: '#c44030' },
    { key: 'longest_session', icon: 'bi-lightning-fill', label: 'Longest Session', color: '#e05540' },
    { key: 'streak',          icon: 'bi-fire',           label: 'Longest Streak',  color: '#c44030' },
  ];
  const MEDALS = ['🥇', '🥈', '🥉'];

  function renderStats(container, data) {
    if (!container) return;
    container.innerHTML = STAT_SECTIONS.map(sec => {
      const rows = data[sec.key] || [];
      const rowsHtml = rows.length === 0
        ? '<div class="stats-empty">No data yet</div>'
        : rows.map((r, i) => `
            <div class="stats-row">
              <span class="stats-rank">${MEDALS[i] || ''}</span>
              <span class="stats-name">${escHtml(r.name)}</span>
              <span class="stats-value">${escHtml(r.value)}</span>
            </div>`).join('');
      return `
        <div class="stats-section-header" style="color:${sec.color}">
          <i class="bi ${sec.icon}"></i>${sec.label}
        </div>
        ${rowsHtml}`;
    }).join('');
  }

  // ── SSE ──────────────────────────────────────────────────────────────────
  // A revoked display would otherwise sit in a 3s reconnect loop against a 403
  // while showing stale names. After a few consecutive failures, reload — the
  // page gate then lands it on the pairing screen, which is the truth.
  const MAX_SSE_FAILURES = 3;

  function connectSSE(url, handlers) {
    let failures = 0;
    (function connect() {
      const es = new EventSource(url);
      es.onopen = () => { failures = 0; };
      Object.entries(handlers).forEach(([event, fn]) => es.addEventListener(event, fn));
      es.onerror = () => {
        es.close();
        failures += 1;
        if (failures >= MAX_SSE_FAILURES) { location.reload(); return; }
        setTimeout(connect, 3000);
      };
    })();
  }

  // ── Boards ───────────────────────────────────────────────────────────────
  function makeBoard(root, spec) {
    return {
      root,
      count: spec.initialCount || 0,
      isEmpty() { return this.count === 0; },
      async refresh() {
        if (!this.root) return;
        try { await spec.refresh.call(this); } catch (_) { /* next tick retries */ }
      },
      async refreshStats() {
        if (!this.root) return;
        try { await spec.refreshStats.call(this); } catch (_) { /* silent */ }
      },
    };
  }

  function studentBoard(root, options) {
    const opts = options || {};
    return makeBoard(root, {
      initialCount: opts.initialCount,
      async refresh() {
        const data = await (await fetch('/kiosk/student/data')).json();
        let total = 0;
        for (const [teamNum, students] of Object.entries(data)) {
          const listEl = this.root.querySelector(`[data-list="${teamNum}"]`);
          const countEl = this.root.querySelector(`[data-count="${teamNum}"]`);
          if (!listEl) continue;               // e.g. the 403 body's "detail" key
          total += students.length;
          if (countEl) countEl.textContent = students.length;
          listEl.innerHTML = students.length === 0
            ? '<div class="empty-state">No one signed in yet</div>'
            : students.map(s => `
                <div class="person-row">
                  <span class="person-name">${escHtml(s.name)}</span>
                  <span class="person-meta">${escHtml(s.sign_in_time)} &nbsp;·&nbsp; ${escHtml(s.elapsed)}</span>
                </div>`).join('');
          startAutoScroll(listEl);
        }
        this.count = total;
      },
      async refreshStats() {
        const data = opts.demoStats || await (await fetch('/kiosk/student/stats')).json();
        renderStats(this.root.querySelector('[data-stats-content]'), data);
        const byName = Object.fromEntries((data.team_totals || []).map(r => [r.name, r.value]));
        const setFooter = (key, name) => {
          const el = this.root.querySelector(`[data-footer="${key}"]`);
          if (el) el.textContent = byName[name] || '—';
        };
        setFooter('4143', 'Team 4143');
        setFooter('4423', 'Team 4423');
        setFooter('combined', 'Combined');
      },
    });
  }

  function mentorBoard(root, options) {
    const opts = options || {};
    return makeBoard(root, {
      initialCount: opts.initialCount,
      async refresh() {
        const data = await (await fetch('/kiosk/mentor/data')).json();
        const mentors = data.signed_in || [];
        this.count = mentors.length;
        const listEl = this.root.querySelector('[data-list="signed-in"]');
        const countEl = this.root.querySelector('[data-count="signed-in"]');
        if (countEl) countEl.textContent = mentors.length;
        if (!listEl) return;
        listEl.innerHTML = mentors.length === 0
          ? '<div class="empty-state">No mentors signed in yet</div>'
          : mentors.map(m => {
              const meta = (m.team ? `Team ${escHtml(m.team)} &nbsp;·&nbsp; ` : '') + escHtml(m.elapsed);
              return `<div class="person-row">
                <span class="person-name">${escHtml(m.name)}</span>
                <span class="person-meta">${meta}</span>
              </div>`;
            }).join('');
        startAutoScroll(listEl);
      },
      async refreshStats() {
        const data = await (await fetch('/kiosk/mentor/stats')).json();
        renderStats(this.root.querySelector('[data-stats-content]'), data);
      },
    });
  }

  // ── Badge scanner ────────────────────────────────────────────────────────
  // The scanner is a keyboard: it types the badge id and presses Enter. Keep
  // focus pinned to the hidden input so a stray click can't swallow a scan.
  //
  // `onResult(data)`, if given, fires with the parsed SignInResponse after every
  // successful fetch (not on a network error, which has no response to act on).
  // The combined page uses it to decide the student/mentor board swap locally,
  // from this browser's own scan — the pinned single-board pages have nothing to
  // swap, so they call this with no argument.
  function initBadgeScanner(onResult) {
    const input = document.getElementById('badge-input');
    const toast = document.getElementById('feedback-toast');
    const msgEl = document.getElementById('feedback-msg');
    if (!input) return;

    document.addEventListener('click', () => input.focus());
    document.addEventListener('keydown', () => input.focus());

    let toastTimer = null;
    function showFeedback(success, msg) {
      if (!toast) return;
      toast.className = 'alert shadow-lg ' + (success ? 'alert-success' : 'alert-danger');
      msgEl.textContent = msg;
      toast.style.display = 'block';
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toast.style.display = 'none'; }, 4000);
    }

    input.addEventListener('keydown', async (e) => {
      if (e.key !== 'Enter') return;
      const badgeId = input.value.trim();
      input.value = '';
      if (!badgeId) return;
      try {
        const resp = await fetch('/kiosk/signin', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: badgeId }),
        });
        const data = await resp.json();
        showFeedback(data.success, data.message);
        if (onResult) onResult(data);
      } catch (err) {
        showFeedback(false, 'Connection error. Please try again.');
      }
    });
  }

  return {
    escHtml, startClock, startAutoScroll, initScrollers,
    renderStats, connectSSE, studentBoard, mentorBoard, initBadgeScanner,
  };
})();

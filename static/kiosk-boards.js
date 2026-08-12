/* Shared rendering + refresh logic for the kiosk display boards.
 *
 * Used by three pages: the combined auto-swapping display (/kiosk) and the two
 * pinned views (/kiosk/student, /kiosk/mentor). Boards are bound to a root
 * element and address their parts through data- attributes rather than global
 * ids, so both can coexist in one document on the combined page.
 *
 * Each board exposes refresh(); the combined page's swap controller is built
 * on top of it, triggered by initBadgeScanner's / initCameraScanner's optional
 * onResult callback (this browser's own scan) rather than by the SSE events
 * also handled below, which are data-refresh only.
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
    // Each category is one .stats-cell — a self-contained header+rows block
    // — rather than flat siblings, so .stats-grid (kiosk.css) can lay the
    // four of them out as a 2x2 grid instead of one long stack. Order here
    // (alltime, week, longest_session, streak) auto-flows into top-left/
    // top-right/bottom-left/bottom-right.
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
        <div class="stats-cell">
          <div class="stats-section-header" style="color:${sec.color}">
            <i class="bi ${sec.icon}"></i>${sec.label}
          </div>
          ${rowsHtml}
        </div>`;
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
      async refresh() {
        const data = await (await fetch('/kiosk/student/data')).json();
        for (const [teamNum, students] of Object.entries(data)) {
          const listEl = this.root.querySelector(`[data-list="${teamNum}"]`);
          const countEl = this.root.querySelector(`[data-count="${teamNum}"]`);
          if (!listEl) continue;               // e.g. the 403 body's "detail" key
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

  function mentorBoard(root) {
    return makeBoard(root, {
      async refresh() {
        const data = await (await fetch('/kiosk/mentor/data')).json();
        const mentors = data.signed_in || [];
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

  // ── Badge submission (shared by the wedge scanner and the camera) ─────────
  // Two input paths reach this: the hardware keyboard-wedge scanner (typed +
  // Enter, see initBadgeScanner) and the kiosk webcam (see initCameraScanner).
  // Both submit the same thing — the bare `member_code` string the QR encodes —
  // to the same endpoint, and share one toast, so a student can't tell which
  // path served them.
  //
  // `onResult(data)`, if given, fires with the parsed SignInResponse after every
  // successful fetch (not on a network error, which has no response to act on).
  // The combined page uses it to decide the student/mentor board swap locally,
  // from this browser's own scan — the pinned single-board pages have nothing to
  // swap, so they call the initialisers with no argument.
  let _toastTimer = null;
  function showFeedback(success, msg) {
    // Looked up per call rather than captured at init: the wedge and camera
    // initialise independently, and either may be the first (or only) caller.
    const toast = document.getElementById('feedback-toast');
    const msgEl = document.getElementById('feedback-msg');
    if (!toast || !msgEl) return;
    toast.className = 'alert shadow-lg ' + (success ? 'alert-success' : 'alert-danger');
    msgEl.textContent = msg;
    toast.style.display = 'block';
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { toast.style.display = 'none'; }, 4000);
  }

  // ── Scanner beep ─────────────────────────────────────────────────────────
  // Neither input path (webcam, keyboard-wedge) makes a sound on its own, so
  // play one of three short WAV clips (static/sounds/) distinct enough that
  // a mentor watching a line of students scan through can tell sign-ins from
  // sign-outs from a bad read without looking up at the screen.
  const BEEP_SOUNDS = {
    in: new Audio('/static/sounds/in.wav'),
    out: new Audio('/static/sounds/out.wav'),
    error: new Audio('/static/sounds/error.wav'),
  };
  // Easter eggs: on a plain accepted sign-in (never sign-out or error, so the
  // functional meaning of the beep is never in question), occasionally swap
  // the normal chime for one of these instead. Kept as filenames rather than
  // Audio objects so EASTER_EGG_SOUNDS and _unlockBeeps below stay in sync.
  const EASTER_EGG_FILES = [
    'match_endgame', 'start', 'resume', 'shift_change',
    'timeout_warning', 'warning_guitar', 'warning_sonar',
  ];
  const EASTER_EGG_SOUNDS = EASTER_EGG_FILES.map((name) => new Audio(`/static/sounds/${name}.wav`));
  const EASTER_EGG_CHANCE = 1.0/50.0; // ~1 in 50 sign-ins

  // This kiosk's first accepted sign-in of the day gets the match-warmup horn
  // instead — a one-per-day fanfare, separate from the random pool above.
  // "First" is tracked per browser (localStorage), not globally across every
  // kiosk station, since there's no server round trip backing this — it's
  // just whichever scan this screen happens to see first each day.
  const MATCH_WARMUP_SOUND = new Audio('/static/sounds/match_warmup.wav');
  const WARMUP_DATE_KEY = 'tempus_kiosk_warmup_date';
  function _isFirstSignInToday() {
    const today = new Date().toDateString();
    try {
      if (localStorage.getItem(WARMUP_DATE_KEY) === today) return false;
      localStorage.setItem(WARMUP_DATE_KEY, today);
      return true;
    } catch (_) {
      return false; // storage disabled (private browsing, etc.) — just skip the fanfare
    }
  }

  // Autoplay policy blocks the *first* play() on some browsers until a user
  // gesture has touched the page, so prime every clip off the first
  // pointerdown or keydown anywhere on the kiosk — including the wedge
  // scanner's own Enter keystroke — well before a camera-triggered scan
  // needs one, which has no gesture of its own to ride in on.
  function _unlockBeeps() {
    // Muted for the round trip: play() resolving and the pause() that follows
    // are normally close enough together to be an inaudible blip, but a
    // keydown can be F11, whose fullscreen transition is heavy enough to
    // delay that pause — muting means a stretched-out gap still can't be
    // heard instead of playing all clips at once.
    Object.values(BEEP_SOUNDS).concat(EASTER_EGG_SOUNDS, [MATCH_WARMUP_SOUND]).forEach((a) => {
      const vol = a.volume;
      a.volume = 0;
      const restore = () => { a.pause(); a.currentTime = 0; a.volume = vol; };
      a.play().then(restore).catch(restore);
    });
  }
  document.addEventListener('pointerdown', _unlockBeeps, { once: true });
  document.addEventListener('keydown', _unlockBeeps, { once: true });

  function playBeep(kind) {
    let src = BEEP_SOUNDS[kind];
    if (kind === 'in') {
      if (_isFirstSignInToday()) {
        src = MATCH_WARMUP_SOUND;
      } else if (Math.random() < EASTER_EGG_CHANCE) {
        src = EASTER_EGG_SOUNDS[Math.floor(Math.random() * EASTER_EGG_SOUNDS.length)];
      }
    }
    if (!src) return;
    // Clone so a beep still playing from the previous scan doesn't get cut
    // off by the next one.
    src.cloneNode(true).play().catch(() => {});
  }

  async function submitBadge(code, onResult) {
    try {
      const resp = await fetch('/kiosk/signin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: code }),
      });
      const data = await resp.json();
      showFeedback(data.success, data.message);
      playBeep(!data.success ? 'error' : (data.is_sign_out ? 'out' : 'in'));
      if (onResult) onResult(data);
    } catch (err) {
      showFeedback(false, 'Connection error. Please try again.');
      playBeep('error');
    }
  }

  // ── Wedge (keyboard) badge scanner ───────────────────────────────────────
  // The handheld scanner is a keyboard: it types the badge id and presses Enter.
  // Keep focus pinned to the hidden input so a stray click can't swallow a scan.
  function initBadgeScanner(onResult) {
    const input = document.getElementById('badge-input');
    if (!input) return;

    // …except inside an element that opts out. The camera HUD marks itself
    // data-kiosk-interactive so a control there isn't fought for focus by the
    // very click that activated it.
    const refocus = (e) => {
      const t = e.target;
      if (t instanceof Element && t.closest('[data-kiosk-interactive]')) return;
      input.focus();
    };
    document.addEventListener('click', refocus);
    document.addEventListener('keydown', refocus);

    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      const badgeId = input.value.trim();
      input.value = '';
      if (!badgeId) return;
      submitBadge(badgeId, onResult);
    });
  }

  // ── Camera badge scanner ─────────────────────────────────────────────────
  // A webcam on the kiosk PC watching for QR badges on students' phone screens.
  // Hands-free: there is no button, and no "aim and press". Coexists with the
  // handheld wedge scanner above — both feed submitBadge().
  //
  // Library: nimiq/qr-scanner 1.4.2 (MIT), vendored under
  // /static/vendor/qr-scanner/ — see that directory's LICENSE.

  // A code counts again only after it has been *out of the camera's view* for
  // this long. Not "N seconds since the last submit": the camera re-decodes the
  // same badge every frame it is visible, so a fixed cooldown would fire again
  // the moment it expired. With a badge propped in front of the lens that means
  // sign-in → (60s server debounce) → sign-out → sign-in → …, manufacturing a
  // fake session every minute. Refreshing the timestamp on *every* sighting
  // makes the gate un-openable until the badge physically leaves, so one
  // showing is exactly one action — and a sign-out is not followed by an
  // instant re-sign-in, which sign_in()'s toggle would otherwise happily do.
  const SCAN_DEBOUNCE_MS = 4000;

  // Field-tested: throttling below the library's own default (25) made pickup
  // feel sluggish with a line of people moving through, and decoding happens in
  // a Web Worker off the main thread, so it doesn't compete with the boards'
  // requestAnimationFrame auto-scroll the way a main-thread cost would.
  const CAMERA_SCANS_PER_SECOND = 25;

  // No camera, denied permission, or an unplugged webcam: retry quietly rather
  // than requiring someone to reload a wall display.
  const CAMERA_RETRY_MS = 60000;

  // `onResult(data)` — same contract as initBadgeScanner. Returns the QrScanner
  // instance, or null if the page has no camera markup (see below) or the
  // vendored library failed to load.
  function initCameraScanner(onResult) {
    const hud    = document.getElementById('camera-hud');
    const video  = document.getElementById('camera-video');
    const status = document.getElementById('camera-status');
    // No markup, no camera. This is how /kiosk/demo (ungated, no pairing) and
    // /kiosk/mentor (no badge input at all) opt out — nothing to configure.
    if (!hud || !video) return null;

    const setState = (state, text) => {
      hud.dataset.state = state;
      if (status) status.textContent = text;
    };

    if (!window.QrScanner) {           // vendored script missing or blocked
      hud.hidden = false;
      setState('off', 'Scanner unavailable');
      return null;
    }

    // decoded text -> ms timestamp of its most recent sighting
    const lastSeen = new Map();

    function onDecode(result) {
      // returnDetailedScanResult gives { data, cornerPoints }; the QR encodes a
      // bare 8-hex-char member_code, which goes to the server verbatim.
      const code = String((result && result.data) || '').trim();
      if (!code) return;
      const now = Date.now();
      const seenAt = lastSeen.get(code) || 0;
      lastSeen.set(code, now);                       // refresh on EVERY frame
      if (now - seenAt < SCAN_DEBOUNCE_MS) return;   // still in view / just left
      if (lastSeen.size > 200) {                     // a display runs for weeks
        for (const [c, t] of lastSeen) {
          if (now - t > SCAN_DEBOUNCE_MS * 10) lastSeen.delete(c);
        }
      }
      submitBadge(code, onResult);
    }

    // Reveal the HUD *before* constructing: QrScanner inspects the video's
    // computed style on the next animation frame and, if it finds it hidden,
    // force-overrides display/visibility and throws away its scan-region
    // overlay. (This is also why `hidden` goes on #camera-hud, never on
    // #camera-video itself.)
    hud.hidden = false;
    setState('starting', 'Starting camera…');

    const scanner = new window.QrScanner(video, onDecode, {
      returnDetailedScanResult: true,
      maxScansPerSecond: CAMERA_SCANS_PER_SECOND,
      highlightScanRegion: true,   // draws the target box inside the preview
      onDecodeError: () => {},     // "No QR code found" fires every frame; the
                                    // default handler would log for 12 hours
      // The library's own default scan region is a centered square of just 2/3
      // of the shorter video dimension — field-tested and it made the camera
      // feel unresponsive, since a badge held anywhere but dead-center was
      // never even looked at. Scan nearly the full frame instead, with a small
      // margin since a badge held right at the very edge is usually clipped
      // anyway. Also raise the downscale target above the library's 400px
      // default (but never upscale past the source) — scanning a bigger area
      // at the same 400px budget would otherwise cost the resolution back.
      calculateScanRegion: (video) => {
        const width = Math.round(video.videoWidth * 0.9);
        const height = Math.round(video.videoHeight * 0.9);
        const scale = Math.min(1, 600 / Math.max(width, height));
        return {
          x: Math.round((video.videoWidth - width) / 2),
          y: Math.round((video.videoHeight - height) / 2),
          width,
          height,
          downScaledWidth: Math.round(width * scale),
          downScaledHeight: Math.round(height * scale),
        };
      },
    });

    let retryTimer = null;
    const retryLater = () => {
      if (retryTimer) return;
      retryTimer = setTimeout(() => { retryTimer = null; start(); }, CAMERA_RETRY_MS);
    };

    async function start() {
      setState('starting', 'Starting camera…');
      // getUserMedia does not exist off a secure origin — e.g. testing from a
      // phone against http://<lan-ip>:8000. Terminal: retrying can't help.
      if (!window.isSecureContext || !navigator.mediaDevices) {
        setState('off', 'Camera needs HTTPS');
        return;
      }
      // hasCamera() enumerates devices without prompting, so this separates
      // "no webcam plugged in" from "permission denied" — start() itself throws
      // the string 'Camera not found.' for both.
      if (!(await window.QrScanner.hasCamera())) {
        setState('off', 'No camera detected');
        retryLater();
        return;
      }
      try {
        await scanner.start();
        setState('on', 'Show your badge');
        // A webcam unplugged mid-shift ends the track silently; without this the
        // preview freezes on the last frame and looks alive.
        const stream = video.srcObject;
        if (stream && stream.getVideoTracks) {
          const [track] = stream.getVideoTracks();
          if (track) track.addEventListener('ended', () => {
            setState('off', 'Camera disconnected');
            retryLater();
          });
        }
      } catch (err) {
        setState('off', 'Camera blocked — use the handheld scanner');
        retryLater();
      }
    }

    start();
    return scanner;
  }

  // ── Camera preview show/hide (kiosk-instance-local) ────────────────────────
  // The scanner itself never stops — this only toggles the visual video feed,
  // so a busy roster panel doesn't keep losing its bottom rows to a preview
  // box no one happens to be aiming a phone at right now (see
  // .kiosk-panel-reserve-camera / .camera-frame-clip in kiosk.css). Persisted
  // in this browser's own localStorage rather than a server setting: the
  // point is that each physical kiosk remembers its own preference — a shop
  // with several displays might want the preview up on one and tucked away
  // on another.
  const CAMERA_PREVIEW_HIDDEN_KEY = 'tempus.kiosk.cameraPreviewHidden';

  function initCameraPreviewToggle() {
    const hud = document.getElementById('camera-hud');
    const btn = document.getElementById('camera-preview-toggle');
    if (!hud || !btn) return;
    const icon = btn.querySelector('i');

    const apply = (hide) => {
      hud.dataset.preview = hide ? 'hidden' : 'shown';
      btn.classList.toggle('active', hide);
      btn.setAttribute('aria-pressed', String(hide));
      btn.title = hide ? 'Show camera preview' : 'Hide camera preview';
      if (icon) icon.className = hide ? 'bi bi-camera-video-off' : 'bi bi-camera-video';
    };

    apply(localStorage.getItem(CAMERA_PREVIEW_HIDDEN_KEY) === '1');

    btn.addEventListener('click', () => {
      const hide = hud.dataset.preview !== 'hidden';
      localStorage.setItem(CAMERA_PREVIEW_HIDDEN_KEY, hide ? '1' : '0');
      apply(hide);
    });
  }

  return {
    escHtml, startClock, startAutoScroll, initScrollers,
    renderStats, connectSSE, studentBoard, mentorBoard,
    initBadgeScanner, initCameraScanner, initCameraPreviewToggle,
  };
})();

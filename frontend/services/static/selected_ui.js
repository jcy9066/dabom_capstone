/* Selected dashboard UI interactions: visual-only bindings to existing state.
 * No React runtime or changes to robot/network control flows.
 */
(function selectedDashboardUI(global) {
  'use strict';

  const ui = global.DabomSelectedUI = global.DabomSelectedUI || {};
  const reduceMotion = global.matchMedia?.('(prefers-reduced-motion: reduce)');
  const $ = selector => document.querySelector(selector);
  const $$ = selector => Array.from(document.querySelectorAll(selector));

  /* The alert confirmation is used ONLY at existing delete confirmation steps. */
  ui.confirmDelete = function confirmDelete(message) {
    if (!document.body) return Promise.resolve(global.confirm(message));
    return new Promise(resolve => {
      const previous = document.activeElement;
      const overlay = document.createElement('div');
      overlay.className = 'ui-confirm-overlay';
      overlay.innerHTML = '<section class="ui-confirm-card" role="alertdialog" aria-modal="true" aria-labelledby="ui-confirm-title" aria-describedby="ui-confirm-message">'
        + '<div class="ui-confirm-symbol" aria-hidden="true">!</div>'
        + '<h2 class="ui-confirm-title" id="ui-confirm-title">삭제 확인</h2>'
        + '<p class="ui-confirm-message" id="ui-confirm-message"></p>'
        + '<div class="ui-confirm-actions"><button type="button" data-ui-confirm="no">취소</button>'
        + '<button type="button" class="ui-confirm-delete" data-ui-confirm="yes">삭제</button></div></section>';
      overlay.querySelector('#ui-confirm-message').textContent = String(message || '선택 항목을 삭제하시겠습니까?');
      const cancel = overlay.querySelector('[data-ui-confirm="no"]');
      const approve = overlay.querySelector('[data-ui-confirm="yes"]');
      let completed = false;
      function finish(confirmed) {
        if (completed) return;
        completed = true;
        document.removeEventListener('keydown', onKey, true);
        overlay.remove();
        if (previous && previous.isConnected) previous.focus?.();
        resolve(confirmed);
      }
      function onKey(event) {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finish(false); }
        if (event.key === 'Tab') {
          if (event.shiftKey && document.activeElement === cancel) { event.preventDefault(); approve.focus(); }
          else if (!event.shiftKey && document.activeElement === approve) { event.preventDefault(); cancel.focus(); }
        }
      }
      cancel.addEventListener('click', () => finish(false));
      approve.addEventListener('click', () => finish(true));
      overlay.addEventListener('click', event => { if (event.target === overlay) finish(false); });
      document.body.appendChild(overlay);
      document.addEventListener('keydown', onKey, true);
      cancel.focus();
    });
  };

  /* Pi badge is driven by the existing, server-synchronized state, NOT by camera. */
  function installPiIndicator() {
    const state = $('#pi-connection-status');
    const badge = $('#pi-live-badge');
    if (!state || !badge) return;
    function sync() {
      const text = (state.textContent || '').trim().toUpperCase();
      const online = state.classList.contains('state-online') && /^(ON|ONLINE|CONNECTED)$/i.test(text);
      badge.classList.toggle('ui-pi-online', online);
      badge.setAttribute('aria-label', online ? 'Raspberry Pi 연결됨' : 'Raspberry Pi 연결되지 않음 또는 상태 확인 불가');
      const label=badge.querySelector('.ui-pi-label');
      if (label) label.textContent = online ? 'ON' : (text === 'OFFLINE' ? 'OFF' : 'UNKNOWN');
    }
    const obs = new MutationObserver(sync);
    obs.observe(state, { childList:true, characterData:true, subtree:true, attributes:true, attributeFilter:['class'] });
    sync();
  }

  function installHamburger() {
    const button = $('#menuBtn');
    if (!button) return;
    function sync(open) { button.classList.toggle('ui-open', Boolean(open)); button.setAttribute('aria-expanded', String(Boolean(open))); }
    document.addEventListener('dabom:sidebar-visibility', event => sync(event.detail?.open));
    sync($('#sidebar')?.classList.contains('open'));
  }

  /* Rubber Segment animation overlays current FREE/TOP/FOLLOW without hijacking clicks. */
  function installViewerSegment() {
    const container = $('.lidar-view-controls');
    if (!container) return;
    const buttons = $$('[data-lidar-view]').filter(btn => container.contains(btn));
    if (!buttons.length) return;
    const indicator = document.createElement('span');
    indicator.className = 'ui-rubber-indicator';
    indicator.setAttribute('aria-hidden', 'true');
    container.prepend(indicator);
    let scheduled = false;
    function update() {
      scheduled = false;
      const button = buttons.find(b => b.classList.contains('active'));
      if (!button) { indicator.hidden = true; return; }
      indicator.hidden = false;
      const parentRect = container.getBoundingClientRect();
      const rect = button.getBoundingClientRect();
      container.style.setProperty('--ui-segment-top', (rect.top-parentRect.top) + 'px');
      container.style.setProperty('--ui-segment-left', (rect.left-parentRect.left) + 'px');
      container.style.setProperty('--ui-segment-width', rect.width + 'px');
      container.style.setProperty('--ui-segment-height', rect.height + 'px');
    }
    function schedule() {
      if (scheduled) return;
      scheduled = true;
      global.requestAnimationFrame(update);
    }
    const observer = new MutationObserver(schedule);
    for (const button of buttons) observer.observe(button,{ attributes:true, attributeFilter:['class','hidden','style'] });
    if (global.ResizeObserver) new ResizeObserver(schedule).observe(container);
    global.addEventListener('resize',schedule,{ passive:true });
    global.addEventListener('fullscreenchange',schedule);
    schedule();
  }

  /* Animate each newly observed alert once, never every periodic reconciliation. */
  function installAlertEntrance() {
    const root = $('#alertBox');
    if (!root) return;
    const seen = new Set();
    const key = item => item.dataset.navigationHazard ||
      ((item.querySelector('.alert-time')?.textContent || '') + '|' + (item.querySelector('.alert-message')?.textContent || ''));
    root.querySelectorAll('.alert-entry').forEach(item => seen.add(key(item)));
    const observer = new MutationObserver(mutations => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.nodeType !== 1) continue;
          const rows = node.matches?.('.alert-entry') ? [node] : Array.from(node.querySelectorAll?.('.alert-entry') || []);
          for (const item of rows) {
            const id = key(item);
            if (seen.has(id)) continue;
            seen.add(id);
            if (seen.size > 500) seen.delete(seen.values().next().value);
            if (!reduceMotion?.matches) item.classList.add('ui-alert-enter');
          }
        }
      }
    });
    observer.observe(root,{childList:true,subtree:true});
  }

  /* Status Mark: read existing server-valued text, do not infer success from missing data. */
  function installStatusMarks() {
    const root = $('#serverStateCard');
    if (!root) return;
    function update() {
      // Only genuine states: map names, goals and path descriptions are values, not statuses.
      root.querySelectorAll('#navigation-state-status, #emergency-stop-status').forEach(value => {
        const raw = (value.textContent || '').trim().toUpperCase();
        let status = 'unknown';
        if (value.classList.contains('state-danger') || /\b(FAIL|FAILED|ERROR|OFFLINE|FAULT|EMERGENCY|BLOCKED)\b/.test(raw)) status='error';
        else if (value.classList.contains('state-success') || raw === 'CLEAR') status='success';
        else if (/\b(STARTING|PENDING|LOADING|WAITING|RUNNING)\b/.test(raw)) status='pending';
        else if (/\b(READY|ONLINE|CONNECTED|SUCCESS|COMPLETED)\b/.test(raw)) status='success';
        value.classList.add('ui-state-marked');
        value.dataset.uiState = status;
      });
    }
    const observer = new MutationObserver(update);
    observer.observe(root,{subtree:true,childList:true,characterData:true});
    update();
  }

  /* Small dot loader is attached to existing loading text, not to new fake states. */
  function installExistingLoaders() {
    const selector = '.gallery-empty.is-loading,.table-empty.is-loading,.saved-map-progress,.record-delete-status,.modal-body .record-loading,[aria-busy="true"],#server-state-updated,.system-control-refresh';
    function sync(root) {
      if (!root?.querySelectorAll) return;
      root.querySelectorAll(selector).forEach(element => {
        if (element.dataset.uiLoadingReady !== '1') {
          element.dataset.uiLoadingReady = '1';
          const dots = document.createElement('span');
          dots.className = 'ui-dots';
          dots.setAttribute('aria-hidden','true');
          dots.innerHTML='<i></i><i></i><i></i>';
          element.prepend(dots);
        }
        const text = element.textContent.replace(/\s+/g,' ').trim();
        const busy = element.matches('.is-loading,[aria-busy="true"]') ||
          /중입니다|불러오는 중|확인 중|대기 중|LOADING/i.test(text);
        element.classList.toggle('ui-is-loading',busy);
        const dots=element.querySelector('.ui-dots');
        if(dots && dots.hidden !== !busy) dots.hidden = !busy;
      });
    }
    for (const scope of [$('#modalBody'),$('#sidebar'),$('#serverStateCard'),$('#camera-card'),$('.signup-modal')]) {
      if (!scope) continue;
      const observer=new MutationObserver(() => sync(scope));
      observer.observe(scope,{childList:true,characterData:true,subtree:true,attributes:true,attributeFilter:['aria-busy','class']});
      sync(scope);
    }
  }

  /* Presentation-only status visuals from existing connection messages. */
  function installConnectionVisuals() {
    function makeLoader(target) {
      let visual=target.querySelector(':scope > .ui-loading-visual');
      if (!visual) {
        visual=document.createElement('span');
        visual.className='ui-loading-visual';
        visual.setAttribute('aria-hidden','true');
        visual.innerHTML='<span class="ui-dots"><i></i><i></i><i></i></span>';
        target.append(visual);
      }
      return visual;
    }
    function observeStatus(root, getText, live) {
      if (!root) return;
      let syncing=false;
      function sync() {
        if (syncing) return;
        syncing=true;
        try {
          const text=String(getText() || '').toLowerCase();
          const pending=/연결\s*중|연결\s*시도|시작\s*중|확인\s*중|대기\s*중|connecting|starting|waiting|retry/.test(text);
          const online=live();
          const visual=makeLoader(root);
          visual.hidden=online;
          visual.classList.toggle('is-offline',!pending);
          root.dataset.connectionVisual=online?'online':pending?'connecting':'offline';
        } finally {syncing=false;}
      }
      const observer=new MutationObserver(sync);
      observer.observe(root,{attributes:true,attributeFilter:['class','style'],characterData:true,childList:true,subtree:true});
      sync();
    }
    const camera=$('#no-camera-msg');
    observeStatus(camera,()=>camera?.textContent || '',()=>$('#video-wrapper')?.classList.contains('camera-offline')!==true);
    const lidar=$('#lidar-stale-overlay');
    observeStatus(lidar,()=>$('#lidar-stale-title')?.textContent+' '+$('#lidar-stale-detail')?.textContent,
      ()=>!lidar?.classList.contains('visible'));
  }

  function installNullPlaceholders() {
    const bar=$('.system-status-bar');
    if (!bar) return;
    function update() {
      bar.querySelectorAll('span[id]').forEach(node=>{
        if ((node.textContent||'').trim().toLowerCase()==='null') node.textContent='--';
      });
    }
    new MutationObserver(update).observe(bar,{subtree:true,childList:true,characterData:true});
    update();
  }

  function installModalEscape() {
    document.addEventListener('keydown',event => {
      if (event.key !== 'Escape' || $('.ui-confirm-overlay')) return;
      const modal=$('#commonModal');
      if (modal && modal.style.display !== 'none' && global.DabomDashboardComponents?.modal?.getDefault?.()?.activeView) {
        event.preventDefault();
        global.DabomDashboardComponents.modal.getDefault().close();
      }
    });
  }

  function boot() {
    installPiIndicator();
    installHamburger();
    installViewerSegment();
    installAlertEntrance();
    installStatusMarks();
    installExistingLoaders();
    installConnectionVisuals();
    installNullPlaceholders();
    installModalEscape();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',boot,{once:true});
  else boot();
})(window);

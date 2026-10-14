(function initializeImageDetail(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const gallery = components.gallery = components.gallery || {};
    const ACTION_LABELS = Object.freeze({
        WARNING: '경고', MANUAL_MOVING: '수동 주행', REPORT: '신고',
        COMMUNICATION: '직접 통신', NOTE: '상황 기록',
    });
    const state = {
        source: 'all', items: [], selectedIndex: -1, loadState: 'idle', message: '',
        galleryScrollTop: 0,
    };
    let manager = null;
    let registeredManager = null;

    const escapeHtml = value => components.records?.escapeHtml?.(value) ?? String(value ?? '');
    const booleanLabel = (value, yes, no) => components.records?.formatBoolean?.(value, yes, no) ?? '-';

    function itemSource(item) {
        if (item?.source === 'event' || item?.source === 'action') return item.source;
        if (item?.action_id != null) return 'action';
        return 'event';
    }

    function itemId(item) {
        return itemSource(item) === 'event' ? item?.event_id ?? item?.source_id ?? item?.id : item?.action_id ?? item?.source_id ?? item?.id;
    }

    function imageUrl(item) {
        return item?.has_image && item?.image_url ? String(item.image_url) : '';
    }

    function imageMarkup(item, className, alt) {
        const url = imageUrl(item);
        if (!url) {
            return `<span class="${className} gallery-media-fallback" role="img" aria-label="${escapeHtml(alt)}">Image unavailable</span>`;
        }
        return `<img class="${className}" data-gallery-image src="${escapeHtml(url)}" alt="${escapeHtml(alt)}" loading="lazy">`;
    }

    function bindImageFallback(root, onUnavailable) {
        root?.querySelectorAll('[data-gallery-image]').forEach(image => {
            image.addEventListener('error', () => {
                onUnavailable?.(image);
                const fallback = document.createElement('span');
                fallback.className = `${image.className} gallery-media-fallback`;
                fallback.setAttribute('role', 'img');
                fallback.setAttribute('aria-label', image.alt || 'Image unavailable');
                fallback.textContent = 'Image unavailable';
                image.replaceWith(fallback);
            }, { once: true });
        });
    }

    function timestamp(item) {
        return item?.detected_at || item?.created_at || item?.timestamp || `${item?.date || ''} ${item?.time || ''}`.trim() || '-';
    }

    function sourceLabel(item) { return itemSource(item) === 'event' ? '순찰 기록' : '관리자 조치'; }

    function itemLabel(item) {
        return itemSource(item) === 'event'
            ? item?.event_type || '순찰 기록'
            : ACTION_LABELS[item?.action_type] || item?.action_type || '관리자 조치';
    }

    function metadata(item) {
        if (itemSource(item) === 'event') return [
            ['유형', item.event_type],
            ['신뢰도', item.confidence == null ? '-' : `${(Number(item.confidence) * 100).toFixed(1)}%`],
            ['LiDAR X/Y', `${item.lidar_x ?? '-'} / ${item.lidar_y ?? '-'}`],
            ['GPS', `${item.gps_lat ?? '-'} / ${item.gps_lng ?? '-'} / ${item.gps_alt ?? '-'}`],
            ['조치/신고/경고', `${booleanLabel(item.is_resolved, '완료', '미조치')} / ${booleanLabel(item.is_reported, '신고', '미신고')} / ${booleanLabel(item.is_alerted, '경고', '미경고')}`],
            ['오탐', booleanLabel(item.is_false_alarm, '오탐', '정상')],
        ];
        const administrator = item.administrator_name || (item.user_email ? `${item.user_name || item.user_id} (${item.user_email})` : item.user_name || item.user_id);
        return [
            ['관리자', administrator],
            ['조치 유형', ACTION_LABELS[item.action_type] || item.action_type],
            ['관련 이벤트', item.event_id ?? '-'],
            ['내용', item.description_content || '-'],
        ];
    }

    function gallerySnapshot() {
        const grid = manager?.body?.querySelector('[data-gallery-grid]');
        return {
            source: state.source,
            items: state.items.map(item => ({ ...item })),
            loadState: state.loadState,
            message: state.message,
            galleryScrollTop: grid?.scrollTop || 0,
        };
    }

    function restoreGallery(snapshot) {
        if (!snapshot) return;
        state.source = snapshot.source || 'all';
        state.items = Array.isArray(snapshot.items) ? snapshot.items.map(item => ({ ...item })) : [];
        state.loadState = snapshot.loadState || 'ready';
        state.message = snapshot.message || '';
        state.galleryScrollTop = Math.max(0, Number(snapshot.galleryScrollTop) || 0);
        state.selectedIndex = -1;
    }

    function galleryHtml() {
        return `<section class="gallery-view" data-dashboard-modal-view>
            <div class="gallery-toolbar" role="group" aria-label="갤러리 분류">
                <button type="button" class="gallery-filter-btn" data-source="all">전체</button>
                <button type="button" class="gallery-filter-btn" data-source="event">순찰 기록</button>
                <button type="button" class="gallery-filter-btn" data-source="action">관리자 조치</button>
            </div>
            <div class="gallery-grid" data-gallery-grid></div>
        </section>`;
    }

    function restoreGalleryScroll() {
        const apply = () => {
            const grid = manager?.body?.querySelector('[data-gallery-grid]');
            if (grid) grid.scrollTop = state.galleryScrollTop;
        };
        apply();
        if (typeof global.requestAnimationFrame !== 'function') return;
        global.requestAnimationFrame(() => {
            apply();
            global.requestAnimationFrame(apply);
        });
    }

    function renderGallery() {
        const root = manager?.body?.querySelector('.gallery-view');
        const grid = root?.querySelector('[data-gallery-grid]');
        if (!grid) return;
        root.querySelectorAll('[data-source]').forEach(button => button.classList.toggle('active', button.dataset.source === state.source));
        if (state.loadState === 'loading') {
            grid.innerHTML = '<div class="gallery-empty is-loading">갤러리를 불러오는 중입니다.</div>';
            return;
        }
        if (state.loadState === 'error' || state.loadState === 'unavailable') {
            grid.innerHTML = `<div class="gallery-empty is-error">${escapeHtml(state.message || '갤러리를 불러오지 못했습니다.')}</div>`;
            return;
        }
        if (!state.items.length) {
            grid.innerHTML = '<div class="gallery-empty">저장된 이미지가 없습니다.</div>';
            return;
        }
        grid.innerHTML = state.items.map((item, index) => `<button type="button" class="gallery-card" data-gallery-index="${index}">
            ${imageMarkup(item, 'gallery-card-image', `${sourceLabel(item)} image`)}
            <span class="gallery-card-source source-${itemSource(item)}">${escapeHtml(sourceLabel(item))}</span>
            <strong>${escapeHtml(itemLabel(item))}</strong><time>${escapeHtml(timestamp(item))}</time>
        </button>`).join('');
        bindImageFallback(grid, image => {
            const index = Number(image.closest('[data-gallery-index]')?.dataset.galleryIndex);
            if (!Number.isInteger(index) || !state.items[index]) return;
            state.items[index].has_image = false;
            state.items[index].image_url = null;
        });
        grid.querySelectorAll('[data-gallery-index]').forEach(button => {
            button.addEventListener('click', () => openDetail(Number(button.dataset.galleryIndex)));
        });
        restoreGalleryScroll();
    }

    function bindGallery() {
        const root = manager?.body?.querySelector('.gallery-view');
        root?.querySelectorAll('[data-source]').forEach(button => {
            button.addEventListener('click', () => loadGallery(button.dataset.source));
        });
        renderGallery();
    }

    async function loadGallery(source) {
        state.source = source || 'all';
        state.loadState = 'loading';
        state.message = '';
        state.selectedIndex = -1;
        state.galleryScrollTop = 0;
        renderGallery();
        try {
            const response = await fetch(`/api/gallery?source=${encodeURIComponent(state.source)}`, { credentials: 'same-origin', cache: 'no-store' });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
            state.items = Array.isArray(payload) ? payload : (payload.items || payload.rows || payload.records || payload.data || []);
            state.loadState = 'ready';
        } catch (error) {
            state.items = [];
            state.loadState = 'error';
            state.message = error.message || '갤러리를 불러오지 못했습니다.';
        }
        renderGallery();
    }

    function detailHtml(context) {
        const item = context.item;
        const index = Number(context.index);
        const listLength = Number(context.listLength) || 1;
        const allowNavigation = context.origin === 'gallery';
        return `<section class="gallery-detail" data-dashboard-modal-view aria-label="이미지 상세">
            <div class="gallery-detail-header"><strong>이미지 상세</strong><button type="button" class="gallery-detail-back" data-detail-back>← 이전 화면</button></div>
            <div class="gallery-image-zoom">
                <div class="gallery-image-viewport" data-image-viewport tabindex="0" aria-label="이미지 확대 및 이동">
                    ${imageMarkup(item, 'gallery-detail-image', `${sourceLabel(item)} detail image`)}
                </div>
                <div class="gallery-zoom-toolbar" aria-label="이미지 확대 축소">
                    <button type="button" data-image-zoom="-1" aria-label="축소">−</button>
                    <output data-image-zoom-value>100%</output>
                    <button type="button" data-image-zoom="1" aria-label="확대">+</button>
                    <button type="button" data-image-zoom-reset aria-label="기본 크기로 복원">Raw</button>
                </div>
            </div>
            <dl class="gallery-metadata"><div><dt>기록 시각</dt><dd>${escapeHtml(timestamp(item))}</dd></div>${metadata(item).map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? '-')}</dd></div>`).join('')}</dl>
            <div class="gallery-detail-actions">
                ${allowNavigation ? `<button type="button" data-detail-change="-1" ${index <= 0 ? 'disabled' : ''}>← 이전</button>` : '<span></span>'}
                <button type="button" class="gallery-delete-btn" data-detail-delete>삭제</button>
                ${allowNavigation ? `<button type="button" data-detail-change="1" ${index >= listLength - 1 ? 'disabled' : ''}>다음 →</button>` : '<span></span>'}
            </div>
        </section>`;
    }

    function bindImageZoom(root) {
        const viewport = root?.querySelector('[data-image-viewport]');
        const image = viewport?.querySelector('img.gallery-detail-image');
        if (!viewport || !image) return;
        image.draggable = false;
        const output = root.querySelector('[data-image-zoom-value]');
        const down = root.querySelector('[data-image-zoom="-1"]');
        const up = root.querySelector('[data-image-zoom="1"]');
        let scale = 1, x = 0, y = 0, pointer = null;
        const bound = (value, max) => Math.max(-max, Math.min(max, value));
        function apply() {
            const maxX = viewport.clientWidth * (scale - 1) / 2;
            const maxY = viewport.clientHeight * (scale - 1) / 2;
            x = bound(x, maxX); y = bound(y, maxY);
            image.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
            if (output) output.value = `${Math.round(scale * 100)}%`;
            if (down) down.disabled = scale <= 1;
            if (up) up.disabled = scale >= 4;
        }
        function zoom(delta) {
            scale = Math.min(4, Math.max(1, Math.round((scale + delta) * 100) / 100));
            if (scale <= 1) { x = 0; y = 0; }
            apply();
        }
        down?.addEventListener('click', () => zoom(-.25));
        up?.addEventListener('click', () => zoom(.25));
        root.querySelector('[data-image-zoom-reset]')?.addEventListener('click', () => {
            scale = 1; x = 0; y = 0; apply();
        });
        viewport.addEventListener('wheel', event => {
            event.preventDefault();
            zoom(event.deltaY < 0 ? .1 : -.1);
        }, { passive: false });
        viewport.addEventListener('pointerdown', event => {
            if (scale <= 1 || event.button !== 0) return;
            pointer = { id: event.pointerId, clientX: event.clientX, clientY: event.clientY };
            viewport.setPointerCapture?.(event.pointerId);
            viewport.classList.add('is-panning');
        });
        viewport.addEventListener('pointermove', event => {
            if (!pointer || pointer.id !== event.pointerId) return;
            x += event.clientX - pointer.clientX;
            y += event.clientY - pointer.clientY;
            pointer.clientX = event.clientX; pointer.clientY = event.clientY;
            apply();
        });
        const release = () => { pointer = null; viewport.classList.remove('is-panning'); };
        viewport.addEventListener('pointerup', release);
        viewport.addEventListener('pointercancel', release);
        viewport.addEventListener('lostpointercapture', release);
        viewport.addEventListener('keydown', event => {
            if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(.25); }
            else if (event.key === '-') { event.preventDefault(); zoom(-.25); }
            else if (event.key === '0') { event.preventDefault(); scale = 1; x = 0; y = 0; apply(); }
        });
        apply();
    }

    function bindDetail(context) {
        const root = manager?.body?.querySelector('.gallery-detail');
        bindImageFallback(root, () => {
            context.item.has_image = false;
            context.item.image_url = null;
        });
        root?.querySelector('[data-detail-back]')?.addEventListener('click', () => manager.back());
        root?.querySelectorAll('[data-detail-change]').forEach(button => {
            button.addEventListener('click', () => changeDetail(Number(button.dataset.detailChange)));
        });
        root?.querySelector('[data-detail-delete]')?.addEventListener('click', event => deleteItem(context, event.currentTarget));
        bindImageZoom(root);
    }

    async function openDetail(index) {
        if (index < 0 || index >= state.items.length) return;
        state.selectedIndex = index;
        return manager.open('recordImageDetail', { item: state.items[index], index, listLength: state.items.length, origin: 'gallery' });
    }

    function changeDetail(offset) {
        const nextIndex = state.selectedIndex + offset;
        if (nextIndex < 0 || nextIndex >= state.items.length) return;
        state.selectedIndex = nextIndex;
        return manager.open('recordImageDetail', {
            item: state.items[nextIndex], index: nextIndex, listLength: state.items.length, origin: 'gallery',
        }, { replace: true });
    }

    async function deleteItem(context, button) {
        const item = context.item;
        const source = itemSource(item);
        const recordId = itemId(item);
        if (recordId == null) return;
    const approved = window.DabomSelectedUI?.confirmDelete
        ? await window.DabomSelectedUI.confirmDelete('이 이미지를 갤러리에서 삭제하시겠습니까?')
        : confirm('이 이미지를 갤러리에서 삭제하시겠습니까?');
    if (!approved) return;
        const previousText = button.textContent;
        button.disabled = true;
        button.textContent = '삭제 중...';
        try {
            const csrfResponse = await fetch('/api/auth/csrf', { credentials: 'same-origin' });
            const csrf = await csrfResponse.json().catch(() => ({}));
            if (!csrfResponse.ok || !csrf.csrf_token) throw new Error('보안 토큰을 준비하지 못했습니다.');
            const response = await fetch(`/api/gallery/${source}/${encodeURIComponent(recordId)}`, {
                method: 'DELETE', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrf.csrf_token },
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
            const removedIndex = state.items.findIndex(candidate => itemSource(candidate) === source && String(itemId(candidate)) === String(recordId));
            if (removedIndex >= 0) state.items.splice(removedIndex, 1);
            const previous = manager.stack[manager.stack.length - 1];
            if (previous?.name === 'galleryModal' && Array.isArray(previous.state?.items)) {
                const savedIndex = previous.state.items.findIndex(candidate => itemSource(candidate) === source && String(itemId(candidate)) === String(recordId));
                if (savedIndex >= 0) previous.state.items.splice(savedIndex, 1);
            } else if (previous?.state?.rows) {
                const savedRow = previous.state.rows.find(candidate => {
                    const candidateId = source === 'event' ? candidate.event_id : candidate.action_id;
                    return String(candidateId) === String(recordId);
                });
                if (savedRow) {
                    savedRow.has_image = false;
                    savedRow.image_url = null;
                }
            }
            state.selectedIndex = -1;
            await manager.back();
            if (manager.activeView?.name === 'galleryModal') renderGallery();
        } catch (error) {
            alert(error.message || '이미지를 삭제하지 못했습니다.');
            button.disabled = false;
            button.textContent = previousText;
        }
    }

    gallery.registerModalViews = function registerModalViews(modalManager) {
        manager = modalManager;
        if (!manager || registeredManager === manager) return;
        registeredManager = manager;
        manager.register('galleryModal', {
            title: '갤러리',
            render: (context, snapshot) => { restoreGallery(snapshot); return galleryHtml(); },
            onOpen: async (context, snapshot) => { bindGallery(); if (!snapshot) await loadGallery(context.source || 'all'); },
            captureState: gallerySnapshot,
            restoreState: () => bindGallery(),
        });
        manager.register('recordImageDetail', {
            title: '이미지 상세',
            render: context => detailHtml(context),
            onOpen: context => bindDetail(context),
            restoreState: (snapshot, context) => bindDetail(context),
        });
    };

    gallery.openGallery = function openGallery(source = 'all') {
        return manager?.open('galleryModal', { source }, { replace: true });
    };

    gallery.openRecordDetail = function openRecordDetail(item, recordView) {
        if (!item?.image_url) return;
        const source = recordView === 'actionsModal' ? 'action' : 'event';
        return manager?.open('recordImageDetail', { item: { ...item, source }, index: 0, listLength: 1, origin: recordView });
    };

    gallery.mountImageDetail = function mountImageDetail(handlers = {}) {
        const controller = {
            open(index) { return manager ? openDetail(Number(index)) : handlers.open?.(index); },
            back() { return manager?.back() ?? handlers.back?.(); },
            close() { return manager?.close() ?? handlers.close?.(); },
            change(offset) { return manager ? changeDetail(Number(offset)) : handlers.change?.(offset); },
        };
        gallery.imageDetail = controller;
        return controller;
    };

    document.addEventListener('keydown', event => {
        if (manager?.activeView?.name !== 'recordImageDetail' || state.selectedIndex < 0) return;
        if (event.key === 'ArrowLeft') changeDetail(-1);
        if (event.key === 'ArrowRight') changeDetail(1);
    });
})(window);

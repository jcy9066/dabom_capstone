"""Static contract for follow-up UI polishing; runtime acceptance still required."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
def read(path):
    return (ROOT / path).read_text(encoding="utf-8")

LOGIN = read("frontend/templates/login.html")
AUTH = read("frontend/services/static/login_email_auth.js")
CSS = read("frontend/services/static/selected_ui.css")
INDEX = read("frontend/templates/index.html")
MAP = read("frontend/components/navigation/saved_map_modal.js")
SYSTEM = read("frontend/services/static/system_control.js")
RECORDS = read("frontend/components/records/record_modal.js")
GALLERY = read("frontend/components/gallery/image_detail.js")
SITUATION = read("frontend/components/current_situation/current_situation.js")

class CompactUiContracts(unittest.TestCase):
    def test_signup_inputs_are_editable_before_verification(self):
        self.assertIn('id="protectedFields">', LOGIN)
        self.assertNotIn('id="protectedFields" disabled', LOGIN)
        self.assertIn('id="signupSubmit" disabled', LOGIN.replace('type="submit" ', '').replace('class="auth-btn primary signup-submit" ', ''))
        self.assertIn("protectedFields.disabled = false;", AUTH)
        self.assertNotIn("protectedFields.disabled = true;", AUTH)
        self.assertIn("state.verified && state.email === email.value.trim().toLowerCase()", AUTH)
        self.assertIn("submit.disabled = !canSubmit();", AUTH)
        self.assertIn("'/api/auth/email/verify'", AUTH)
        self.assertIn("'/api/auth/register'", AUTH)

    def test_signup_three_rows_and_instructions_removed(self):
        self.assertNotIn("이메일 전체가 로그인 ID입니다.", LOGIN)
        for field, col, row in (
            ("signup-field-name", 2, 1), ("signup-field-password", 1, 2),
            ("signup-field-phone", 2, 2), ("signup-field-confirm", 1, 3),
            ("signup-field-employee", 2, 3),
        ):
            with self.subTest(field=field):
                self.assertIn(f".signup-form > .auth-protected-fields > .{field} {{grid-column:{col};grid-row:{row};}}", CSS)
        self.assertIn(".signup-form > .auth-protected-fields { display:contents; }", CSS)

    def test_server_status_visual_only(self):
        self.assertIn(".server-state-grid .server-state-field strong::first-letter{text-transform:uppercase;}", CSS)
        self.assertIn(".server-state-card .server-state-drawer-toggle:hover", CSS)
        self.assertIn('id="serverStateGrid"', INDEX)

    def test_map_buttons_use_shared_component(self):
        self.assertIn("create('button', 'mb-btn saved-map-start-mapping', labels.startMapping)", MAP)
        self.assertIn("create('button', 'mb-btn saved-map-clear-current', labels.clearCurrentMap)", MAP)
        self.assertIn("startMappingFromModal(container, progress)", MAP)
        self.assertIn("loadSelectedMap(container, progress)", MAP)
        self.assertNotIn(".saved-map-modal .dashboard-control-button", CSS)
        self.assertIn(".saved-map-modal .mb-btn {", CSS)
        self.assertNotIn("function createControlButton(", MAP)
        self.assertIn("min-height:min(60vh,550px)", CSS)

    def test_description_only_on_error(self):
        self.assertIn(".system-control-card .system-control-description{display:none;}", CSS)
        self.assertIn(".system-control-card:is(.has-error,.is-unreachable) .system-control-description{display:block;}", CSS)
        self.assertIn("component.description || ''", SYSTEM)
        self.assertIn("button.disabled = pending || unreachable;", SYSTEM)
        self.assertIn("control(group, component.id, action)", SYSTEM)

    def test_guide_is_two_column_simple_steps(self):
        self.assertIn("grid-auto-flow:column", CSS)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", CSS)
        for title in ("1. 연결 확인", "2. 새 지도 만들기", "3. 저장 지도에서 주행",
                      "4. 위험 알림 대응", "5. 기록 확인", "6. 장비 제어"):
            self.assertIn(title, SYSTEM)
        self.assertIn("manager.open(guideViewName", SYSTEM)

    def test_patrol_table_and_filters(self):
        self.assertIn("records-view-patrolModal .record-data-table :is(th,td)", CSS)
        self.assertIn('class="record-filter-controls"', RECORDS)
        self.assertIn(".record-filter-bar [data-action=\"reset\"]", CSS)
        self.assertIn('data-filter="startAt"', RECORDS)
        self.assertIn('data-filter="endAt"', RECORDS)

    def test_gallery_back_inside_metadata(self):
        self.assertNotIn('gallery-detail-header"><strong>이미지 상세', GALLERY)
        self.assertIn('gallery-metadata-top', GALLERY)
        self.assertIn('data-detail-back>← 이전 화면', GALLERY)
        self.assertIn("manager.back()", GALLERY)
        self.assertIn('data-detail-delete', GALLERY)
        self.assertIn('data-image-zoom-reset', GALLERY)

    def test_situation_form_can_still_save(self):
        self.assertIn("current-situation-bottom-row", SITUATION)
        self.assertIn('id="currentSituationSubmit"', SITUATION)
        self.assertIn('id="currentSituationIncludeImage"', SITUATION)
        self.assertIn('placeholder="현재 상황을 입력하세요.&#10;예:', SITUATION)
        self.assertIn("현재 카메라 이미지를 사용할 수 없습니다.\\n텍스트 기록은 계속할 수 있습니다.", SITUATION)
        self.assertIn("'/api/logs/current-situation/preview'", SITUATION)
        self.assertIn(".current-situation-bottom-row{display:flex;", CSS)

    def test_hover_boundaries_have_safe_insets(self):
        # Hover-lift animation stays active; controls have room to paint above.
        self.assertIn('.gallery-detail .gallery-metadata {', CSS)
        self.assertIn('  padding: 6px 7px 8px;', CSS)
        self.assertIn('.gallery-detail .gallery-metadata-top {', CSS)
        self.assertIn('  padding: 2px 3px 4px;', CSS)
        self.assertIn('.records-view .record-filter-controls,', CSS)
        self.assertIn('.saved-map-modal :is(.saved-map-actions,.saved-map-mode-actions,.saved-map-location-actions)', CSS)
        self.assertIn('  padding-block: 5px;', CSS)
        self.assertNotIn('.saved-map-modal .dashboard-control-button:hover', CSS)

    def test_gallery_source_badge_does_not_overlap_image(self):
        self.assertIn('.gallery-card .gallery-card-source {', CSS)
        self.assertIn('  position: static;', CSS)
        self.assertIn('  order: -1;', CSS)
        self.assertIn('  margin: 8px 10px 0;', CSS)
        self.assertIn("gallery-card-source source-", GALLERY)
        self.assertIn("gallery-card-image", GALLERY)

    def test_server_status_fields_share_the_same_rows(self):
        # Five fixed title/value baselines; no changes to live status IDs.
        self.assertIn('#serverStateGrid .server-state-field {', CSS)
        self.assertIn('  grid-template-rows: 14px minmax(16px, auto);', CSS)
        self.assertIn('  min-height: 48px;', CSS)
        self.assertIn('#serverStateGrid .server-state-field > span {', CSS)
        self.assertIn('#serverStateGrid .server-state-field > strong {', CSS)
        self.assertIn('  display: block;', CSS)
        self.assertIn('#serverStateCard:not(.is-collapsed) .server-state-header {', CSS)
        self.assertIn('margin-bottom: 6px;', CSS)
        self.assertIn('.alert-card{flex:1 1 auto;min-height:0;}', CSS)
        for field_id in ('active-map-status', 'navigation-state-status',
                         'emergency-stop-status', 'current-goal-status', 'planned-path-status'):
            with self.subTest(field=field_id):
                self.assertIn(f'id="{field_id}"', INDEX)

    def test_gallery_footer_text_cannot_shrink_or_clip(self):
        self.assertIn('.gallery-view .gallery-grid {', CSS)
        self.assertIn('  grid-auto-rows: max-content;', CSS)
        self.assertIn('.gallery-view .gallery-card > strong,', CSS)
        self.assertIn('.gallery-view .gallery-card > time {', CSS)
        self.assertIn('  flex: 0 0 auto;', CSS)
        self.assertIn('  min-height: 1.45em;', CSS)
        self.assertIn('.gallery-view .gallery-card > img.gallery-card-image {', CSS)
        self.assertIn('.gallery-view{overflow-y:auto;min-height:0;}', CSS)
        self.assertIn('gallery-card-source source-', GALLERY)
        self.assertIn('<strong>', GALLERY)
        self.assertIn('<time>', GALLERY)

    def test_gallery_first_row_and_invisible_scrollbar(self):
        # The scroll container fills available modal height and sizes full rows to it.
        self.assertIn('#commonModal[data-modal-view="galleryModal"] .modal-body {', CSS)
        self.assertIn('  overflow-y: hidden;', CSS)
        self.assertIn('#commonModal[data-modal-view="galleryModal"] .gallery-view {', CSS)
        self.assertIn('#commonModal[data-modal-view="galleryModal"] .gallery-view .gallery-grid {', CSS)
        self.assertIn('  grid-auto-rows: 100%;', CSS)
        self.assertIn('  overflow-y: auto;', CSS)
        self.assertIn('  scrollbar-width: none;', CSS)
        self.assertIn('  -ms-overflow-style: none;', CSS)
        self.assertIn('.gallery-view .gallery-grid::-webkit-scrollbar,', CSS)
        self.assertIn('  display: none;', CSS)
        self.assertIn('  aspect-ratio: auto;', CSS)
        self.assertIn('  object-fit: cover;', CSS)
        self.assertIn('  object-position: center;', CSS)
        self.assertIn('  background: var(--card-bg2);', CSS)
        # Detail viewer remains uncropped; only gallery thumbnails use cover.
        self.assertIn('object-fit:contain;transform-origin:50% 50%;', CSS)
        self.assertIn('  height: 0;', CSS)
        self.assertIn('  min-height: 0;', CSS)
        self.assertIn('  flex: 1 1 0;', CSS)
        # Existing gallery snapshot restores grid scrollTop and events remain intact.
        self.assertIn('grid.scrollTop = state.galleryScrollTop;', GALLERY)
        self.assertIn("button.addEventListener('click', () => openDetail", GALLERY)
        self.assertIn('gallery-card-source source-', GALLERY)

    def test_ten_records_per_page_and_centered_tables(self):
        pagination = read("frontend/components/records/pagination.js")
        state = read("frontend/services/static/dashboard_state.js")
        self.assertIn("records.DEFAULT_PAGE_SIZE = 10;", pagination)
        self.assertIn("target.hidden = total <= records.DEFAULT_PAGE_SIZE;", pagination)
        self.assertIn("const LOG_API_PAGE_SIZE = 50;", state)
        self.assertIn("query.set('page_size', String(LOG_API_PAGE_SIZE));", state)
        self.assertIn("table :is(th,td) { text-align:center !important;", CSS)
        self.assertIn("record-drive-mode-badge", RECORDS)
        self.assertIn("records.formatBoolean(row.is_autonomous, 'Auto', 'Manual')", RECORDS)

    def test_header_status_drawer_does_not_break_polling(self):
        self.assertIn('id="topSystemStatusBar" data-open="true"', INDEX)
        self.assertIn('data-drawer-target="topSystemStatusBar"', INDEX)
        self.assertIn('id="sys-cpu-usage"', INDEX)
        self.assertIn('id="pi-connection-status"', INDEX)
        self.assertIn('#topSystemStatusBar[data-open="false"]', CSS)
        state = read("frontend/services/static/dashboard_state.js")
        self.assertIn("element.textContent = parsed.toLocaleTimeString", state)
        self.assertNotIn("element.textContent = `확인 ${parsed.", state)

    def test_mobinkakei_loader_animates_three_balls_and_shadows(self):
        visual = read("frontend/services/static/selected_ui.js")
        self.assertIn('<i></i><i></i><i></i><b></b><b></b><b></b>', visual)
        self.assertIn('@keyframes ui-mobinkakei-ball', CSS)
        self.assertIn('@keyframes ui-mobinkakei-shadow', CSS)
        self.assertIn('animation:ui-mobinkakei-ball .5s alternate infinite ease;', CSS)
        self.assertIn('if (element.matches(\'.dashboard-model-toggle\')) return;', visual)
        self.assertIn('.ui-loading-visual.is-offline .ui-dots>:is(i,b){animation:none!important;}', CSS)

    def test_map_cancel_removed_but_others_remain(self):
        self.assertNotIn("actions.append(save, cancel, load)", MAP)
        self.assertIn("actions.append(save, load);", MAP)
        self.assertIn("mapColumn.append(actions, mappingActions);", MAP)
        self.assertIn("startMappingFromModal(container, progress)", MAP)
        self.assertIn("clearCurrentMapFromViewer(progress)", MAP)

    def test_diagnostics_and_bullet_guide_preserved(self):
        self.assertIn("ordered = false", SYSTEM)
        self.assertIn(".system-control-guide {align-items:stretch;", CSS)
        self.assertIn(".system-control-card .system-control-meta {display:none;}", CSS)
        self.assertIn("component.duplicate || component.state === 'duplicate'", SYSTEM)
        self.assertIn("button.disabled = pending || unreachable;", SYSTEM)
        self.assertIn("control(group, component.id, action)", SYSTEM)

    def test_gallery_buttons_and_fixed_report_textarea(self):
        self.assertIn('flex:0 0 max(10%,72px)', CSS)
        self.assertIn('min-width:72px', CSS)
        self.assertIn('id="currentSituationDescription"', SITUATION)
        self.assertIn("textarea.style.overflowY = 'auto';", SITUATION)
        self.assertIn("textarea.style.height = '280px';", SITUATION)
        self.assertIn("height:auto!important;min-height:280px!important;max-height:none!important;", CSS)
        self.assertIn('scrollbar-width:none;-ms-overflow-style:none;', CSS)
        self.assertIn('.lidar-layer-drawer .lidar-display-options label{font-size:7px;', read("frontend/services/static/lidar_3d_viewer.css"))

    def test_updated_resources_referenced(self):
        self.assertIn("selected_ui.css?v=20261015-bookmark-map-buttons-v9", INDEX)
        self.assertIn("selected_ui.css?v=20261015-bookmark-map-buttons-v9", LOGIN)
        self.assertIn("login_email_auth.js?v=20261014-signup-input-v1", LOGIN)
        self.assertIn("saved_map_modal.js?v=20261015-bookmark-buttons-v4", INDEX)
        self.assertIn("current_situation.js?v=20261014-finish-ui-v2", INDEX)
        self.assertIn("record_modal.js?v=20261014-detail-overlay-v1", INDEX)
        self.assertIn("image_detail.js?v=20261014-compact-ui-v2", INDEX)
        self.assertIn("pagination.js?v=20261014-numbered-pages-v1", INDEX)
        self.assertIn("dashboard_state.js?v=20261014-batched-pages-v1", INDEX)
        self.assertIn("system_control.js?v=20261014-hide-pi-service-v1", INDEX)
        self.assertIn("lidar_3d_viewer.css?v=20261014-ux-refine-v17", INDEX)

if __name__ == "__main__":
    unittest.main()

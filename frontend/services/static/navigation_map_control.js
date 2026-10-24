(() => {
    'use strict';

    const navigationMaps = window.DabomDashboardComponents?.navigationMaps;
    window.openSavedMapModal = (...args) => navigationMaps?.controller?.open?.(...args);
})();

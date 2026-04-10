(function() {
    'use strict';
    var MAX_MB = 5;
    var MAX_BYTES = MAX_MB * 1024 * 1024;

    function checkFile(input) {
        if (!input.files || !input.files.length) return;
        var oversized = [];
        for (var i = 0; i < input.files.length; i++) {
            var f = input.files[i];
            if (f.size > MAX_BYTES) {
                oversized.push(f.name + ' (' + (f.size / 1024 / 1024).toFixed(1) + 'MB)');
            }
        }
        // Remove any previous warning for this input
        var prev = input.parentNode.querySelector('.img-size-warn');
        if (prev) prev.remove();

        if (oversized.length) {
            input.value = '';
            var warn = document.createElement('div');
            warn.className = 'img-size-warn';
            warn.style.cssText = 'background:#fef2f2;border:1px solid #fca5a5;color:#991b1b;padding:8px 12px;border-radius:6px;margin-top:6px;font-size:13px;';
            warn.innerHTML = '<strong>File too large:</strong> ' + oversized.join(', ') +
                '<br>Each image must be under ' + MAX_MB + 'MB. Please resize or compress before uploading.';
            input.parentNode.appendChild(warn);
        }
    }

    function init() {
        document.addEventListener('change', function(e) {
            if (e.target && e.target.type === 'file' && e.target.accept && e.target.accept.indexOf('image') !== -1) {
                checkFile(e.target);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

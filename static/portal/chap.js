/*
 * SabiWiFi captive-portal CHAP helper.
 *
 * Submits RADIUS credentials to a MikroTik hotspot using the HTTP-CHAP
 * protocol. The server is provisioned with `login-by=http-chap`, which
 * means MikroTik will REJECT plain-text passwords on the hotspot /login
 * endpoint. Instead, the browser must:
 *
 *   1. Read the CHAP id + challenge from the URL query string
 *      (forwarded here from MikroTik via the per-router hotspot
 *      login.html redirect: see routers/views.py:hotspot_login_html).
 *   2. Compute  md5(chap-id || password || chap-challenge)  and
 *      submit the hex result as the `password` field.
 *
 * chap-id and chap-challenge arrive as hex strings via the URL (how
 * MikroTik emits these variables outside of JS-string context). But
 * the RouterOS hotspot server computes its CHAP response over the
 * RAW BYTES — chap-id is 1 byte, chap-challenge is 16 bytes — so we
 * hex-decode both back to a byte-string before MD5'ing. The password
 * is a normal ASCII string.
 *
 * Embedded MD5: Paul Johnston's implementation (BSD, public-domain
 * compatible) from http://pajhome.org.uk/crypt/md5 — trimmed to the
 * bits we need (ASCII-safe input, hex output).
 */
(function (global) {
    'use strict';

    /* ---------- MD5 (Paul Johnston, trimmed) ---------- */
    function safeAdd(x, y) {
        var lsw = (x & 0xFFFF) + (y & 0xFFFF);
        var msw = (x >> 16) + (y >> 16) + (lsw >> 16);
        return (msw << 16) | (lsw & 0xFFFF);
    }
    function rol(n, c) { return (n << c) | (n >>> (32 - c)); }
    function cmn(q, a, b, x, s, t) {
        return safeAdd(rol(safeAdd(safeAdd(a, q), safeAdd(x, t)), s), b);
    }
    function ff(a, b, c, d, x, s, t) { return cmn((b & c) | ((~b) & d), a, b, x, s, t); }
    function gg(a, b, c, d, x, s, t) { return cmn((b & d) | (c & (~d)), a, b, x, s, t); }
    function hh(a, b, c, d, x, s, t) { return cmn(b ^ c ^ d, a, b, x, s, t); }
    function ii(a, b, c, d, x, s, t) { return cmn(c ^ (b | (~d)), a, b, x, s, t); }

    function coreMD5(x, len) {
        x[len >> 5] |= 0x80 << ((len) % 32);
        x[(((len + 64) >>> 9) << 4) + 14] = len;
        var a = 1732584193, b = -271733879, c = -1732584194, d = 271733878;
        for (var i = 0; i < x.length; i += 16) {
            var oa = a, ob = b, oc = c, od = d;
            a = ff(a, b, c, d, x[i + 0], 7, -680876936);
            d = ff(d, a, b, c, x[i + 1], 12, -389564586);
            c = ff(c, d, a, b, x[i + 2], 17, 606105819);
            b = ff(b, c, d, a, x[i + 3], 22, -1044525330);
            a = ff(a, b, c, d, x[i + 4], 7, -176418897);
            d = ff(d, a, b, c, x[i + 5], 12, 1200080426);
            c = ff(c, d, a, b, x[i + 6], 17, -1473231341);
            b = ff(b, c, d, a, x[i + 7], 22, -45705983);
            a = ff(a, b, c, d, x[i + 8], 7, 1770035416);
            d = ff(d, a, b, c, x[i + 9], 12, -1958414417);
            c = ff(c, d, a, b, x[i + 10], 17, -42063);
            b = ff(b, c, d, a, x[i + 11], 22, -1990404162);
            a = ff(a, b, c, d, x[i + 12], 7, 1804603682);
            d = ff(d, a, b, c, x[i + 13], 12, -40341101);
            c = ff(c, d, a, b, x[i + 14], 17, -1502002290);
            b = ff(b, c, d, a, x[i + 15], 22, 1236535329);
            a = gg(a, b, c, d, x[i + 1], 5, -165796510);
            d = gg(d, a, b, c, x[i + 6], 9, -1069501632);
            c = gg(c, d, a, b, x[i + 11], 14, 643717713);
            b = gg(b, c, d, a, x[i + 0], 20, -373897302);
            a = gg(a, b, c, d, x[i + 5], 5, -701558691);
            d = gg(d, a, b, c, x[i + 10], 9, 38016083);
            c = gg(c, d, a, b, x[i + 15], 14, -660478335);
            b = gg(b, c, d, a, x[i + 4], 20, -405537848);
            a = gg(a, b, c, d, x[i + 9], 5, 568446438);
            d = gg(d, a, b, c, x[i + 14], 9, -1019803690);
            c = gg(c, d, a, b, x[i + 3], 14, -187363961);
            b = gg(b, c, d, a, x[i + 8], 20, 1163531501);
            a = gg(a, b, c, d, x[i + 13], 5, -1444681467);
            d = gg(d, a, b, c, x[i + 2], 9, -51403784);
            c = gg(c, d, a, b, x[i + 7], 14, 1735328473);
            b = gg(b, c, d, a, x[i + 12], 20, -1926607734);
            a = hh(a, b, c, d, x[i + 5], 4, -378558);
            d = hh(d, a, b, c, x[i + 8], 11, -2022574463);
            c = hh(c, d, a, b, x[i + 11], 16, 1839030562);
            b = hh(b, c, d, a, x[i + 14], 23, -35309556);
            a = hh(a, b, c, d, x[i + 1], 4, -1530992060);
            d = hh(d, a, b, c, x[i + 4], 11, 1272893353);
            c = hh(c, d, a, b, x[i + 7], 16, -155497632);
            b = hh(b, c, d, a, x[i + 10], 23, -1094730640);
            a = hh(a, b, c, d, x[i + 13], 4, 681279174);
            d = hh(d, a, b, c, x[i + 0], 11, -358537222);
            c = hh(c, d, a, b, x[i + 3], 16, -722521979);
            b = hh(b, c, d, a, x[i + 6], 23, 76029189);
            a = hh(a, b, c, d, x[i + 9], 4, -640364487);
            d = hh(d, a, b, c, x[i + 12], 11, -421815835);
            c = hh(c, d, a, b, x[i + 15], 16, 530742520);
            b = hh(b, c, d, a, x[i + 2], 23, -995338651);
            a = ii(a, b, c, d, x[i + 0], 6, -198630844);
            d = ii(d, a, b, c, x[i + 7], 10, 1126891415);
            c = ii(c, d, a, b, x[i + 14], 15, -1416354905);
            b = ii(b, c, d, a, x[i + 5], 21, -57434055);
            a = ii(a, b, c, d, x[i + 12], 6, 1700485571);
            d = ii(d, a, b, c, x[i + 3], 10, -1894986606);
            c = ii(c, d, a, b, x[i + 10], 15, -1051523);
            b = ii(b, c, d, a, x[i + 1], 21, -2054922799);
            a = ii(a, b, c, d, x[i + 8], 6, 1873313359);
            d = ii(d, a, b, c, x[i + 15], 10, -30611744);
            c = ii(c, d, a, b, x[i + 6], 15, -1560198380);
            b = ii(b, c, d, a, x[i + 13], 21, 1309151649);
            a = ii(a, b, c, d, x[i + 4], 6, -145523070);
            d = ii(d, a, b, c, x[i + 11], 10, -1120210379);
            c = ii(c, d, a, b, x[i + 2], 15, 718787259);
            b = ii(b, c, d, a, x[i + 9], 21, -343485551);
            a = safeAdd(a, oa);
            b = safeAdd(b, ob);
            c = safeAdd(c, oc);
            d = safeAdd(d, od);
        }
        return [a, b, c, d];
    }

    function str2blks(str) {
        var nblk = ((str.length + 8) >> 6) + 1;
        var blks = new Array(nblk * 16);
        for (var i = 0; i < nblk * 16; i++) blks[i] = 0;
        for (var j = 0; j < str.length; j++) {
            blks[j >> 2] |= (str.charCodeAt(j) & 0xFF) << ((j % 4) * 8);
        }
        return blks;
    }

    function binl2hex(binarray) {
        var hex = '0123456789abcdef';
        var str = '';
        for (var i = 0; i < binarray.length * 4; i++) {
            str += hex.charAt((binarray[i >> 2] >> ((i % 4) * 8 + 4)) & 0xF)
                 + hex.charAt((binarray[i >> 2] >> ((i % 4) * 8)) & 0xF);
        }
        return str;
    }

    function hexMD5(s) { return binl2hex(coreMD5(str2blks(s), s.length * 8)); }

    /* ---------- CHAP helpers ---------- */

    function hexToBytes(hex) {
        var s = '';
        if (!hex) return s;
        for (var i = 0; i + 1 < hex.length; i += 2) {
            s += String.fromCharCode(parseInt(hex.substr(i, 2), 16));
        }
        return s;
    }

    /**
     * Compute the MikroTik HTTP-CHAP response.
     * @param {string} chapId         hex string from $(chap-id) — 2 hex chars = 1 byte
     * @param {string} password       plain-text password (RADIUS auth_token)
     * @param {string} chapChallenge  hex string from $(chap-challenge) — 32 hex chars = 16 bytes
     * @returns {string} MD5 hex response to submit as the `password` field
     */
    function chapResponse(chapId, password, chapChallenge) {
        return hexMD5(hexToBytes(chapId) + (password || '') + hexToBytes(chapChallenge));
    }

    /**
     * Submit a MikroTik hotspot login form using CHAP if chap-id/challenge
     * are present, otherwise fall through with the raw password (PAP — only
     * used during transition; production hotspot profiles are CHAP-only).
     */
    function submitHotspotLogin(form, username, password, dst, chapId, chapChallenge) {
        var pw = (chapId && chapChallenge)
            ? chapResponse(chapId, password, chapChallenge)
            : password;
        form.elements['username'].value = username;
        form.elements['password'].value = pw;
        if (form.elements['dst']) form.elements['dst'].value = dst || '';
        form.submit();
    }

    global.sabiwifiChap = {
        hexMD5: hexMD5,
        chapResponse: chapResponse,
        submitHotspotLogin: submitHotspotLogin,
    };
})(window);

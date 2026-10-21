(function () {
    'use strict';
    var pending = null, generation = 0, locked = false, snapshot = null;
    var saves = Promise.resolve();
    function persist(payload) {
        saves = saves.then(function () {
            return $.ajax({url: Host + '/apm/selection', type: 'POST', data: payload});
        }).then(function (data) {
            if (data.status !== 1) throw new Error(data.msg || 'Process preferences were not saved');
        }).catch(function (error) {
            console.warn(error);
            message(text('进程选择保存失败', 'Could not save process selection'));
        });
        return saves;
    }
    function remember() {
        if (locked || pending || $('#ios-process-select, #ios-network-select').find(':selected[data-invalid]').length) return;
        var device = $('.select-device').val(), pkg = $('.select-app').val();
        if (!device || !pkg) return;
        var cache = getApmSelectionCache();
        var platformCache = cache.iOS = cache.iOS || {};
        var devices = platformCache.devices = platformCache.devices || {};
        var selection = devices[device] = devices[device] || {};
        selection.packageName = pkg;
        var chosen = target();
        selection.iosTargets = {process: getProcessNameFromPid(chosen.process),
            network_process: getProcessNameFromPid(chosen.network_process)};
        platformCache.lastDevice = device;
        saveApmSelectionCache(cache);
        persist({platform: 'iOS', device: device, packageName: pkg, iosTargets: JSON.stringify(selection.iosTargets)});
    }
    function restore() {
        if (locked) return;
        reset();
        var cache = getApmSelectionCache().iOS || {};
        var selection = (cache.devices || {})[$('.select-device').val()] || {};
        var saved = selection.packageName === $('.select-app').val() ? selection.iosTargets : null;
        if (!saved) return;
        [['#ios-process-select', 'process'], ['#ios-network-select', 'network_process']].forEach(function (entry) {
            if (!saved[entry[1]]) return;
            var name = getProcessNameFromPid(saved[entry[1]]);
            var option = new Option(name, name, false, true);
            $(option).attr({'data-invalid': 'true', 'data-restore': 'true'});
            $(entry[0]).append(option).trigger('change.select2');
        });
        if (saved.process || saved.network_process) refresh();
    }
    function select() { return $('#ios-process-select'); }
    function text(cn, en) { return select().data('lan') === 'cn' ? cn : en; }
    function target() {
        var option = select().find(':selected');
        var network = $('#ios-network-select');
        return {process: select().val() || '', process_start: option.attr('data-start') || '',
            network_process: network.val() || '', network_process_start: network.find(':selected').attr('data-start') || ''};
    }
    function message(value) { $('#ios-process-error').text(value || '').prop('hidden', !value); }
    function controls() {
        $('#ios-process-select, #ios-network-select').prop('disabled', locked || !!pending);
        $('#ios-process-refresh').prop('disabled', locked || !!pending);
        $('.select-device, .select-app').prop('disabled', locked);
        $('#debug-connect').toggleClass('disabled', locked).attr('aria-disabled', String(locked));
    }
    function reset() {
        if (locked) return;
        generation++;
        var previous = pending;
        pending = null;
        if (previous) previous.abort();
        select().empty().append(new Option(text('主进程（按包名）', 'Main process (bundle ID)'), ''));
        select().trigger('change.select2');
        $('#ios-network-select').empty().append(new Option(text('跟随采集进程', 'Follow target process'), '')).trigger('change.select2');
        message('');
        controls();
    }
    function refresh() {
        if (locked || pending) return;
        var device = $('.select-device').val(), pkg = $('.select-app').val();
        if (!device || !pkg) {
            message(text('请先选择设备和应用', 'Select a device and app first'));
            return;
        }
        var previous = target(), current = ++generation;
        message('');
        pending = $.ajax({
            url: '/ios/processes', type: 'GET', timeout: 45000,
            data: {device: device, pkgname: pkg},
            success: function (data) {
                if (current !== generation) return;
                if (data.status !== 1) { message(data.msg || text('读取进程失败', 'Process listing failed')); return; }
                ['process', 'network'].forEach(function (kind) {
                    var field = kind === 'process' ? select() : $('#ios-network-select');
                    var saved = kind === 'process' ? previous : {process: previous.network_process, process_start: previous.network_process_start};
                    var restoring = field.find(':selected').attr('data-restore');
                    if (restoring && saved.process) {
                        var matches = data.processes.filter(function (row) { return row.name === getProcessNameFromPid(saved.process); });
                        matches.sort(function (a, b) { return b.pid - a.pid; });
                        if (matches.length) {
                            saved = {process: matches[0].pid + ':' + matches[0].name, process_start: matches[0].start || ''};
                        }
                    }
                    var label = kind === 'process' ? text('主进程（按包名）', 'Main process (bundle ID)') : text('跟随采集进程', 'Follow target process');
                    field.empty().append(new Option(label, ''));
                    var app = $('<optgroup>').attr('label', text('应用进程', 'App processes'));
                    var system = $('<optgroup>').attr('label', text('系统进程（归属待确认）', 'System processes (ownership unverified)'));
                    var found = !saved.process;
                    data.processes.forEach(function (row) {
                        var value = row.pid + ':' + row.name;
                        var option = new Option(value, value);
                        option.setAttribute('data-start', row.start || '');
                        if (value === saved.process && (row.start || '') === saved.process_start) {
                            option.selected = true;
                            found = true;
                        }
                        (row.kind === 'app' ? app : system).append(option);
                    });
                    field.append(app, system);
                    if (!found) {
                        var missing = new Option(text('已失效: ', 'Exited: ') + saved.process, saved.process, false, true);
                        missing.setAttribute('data-invalid', 'true');
                        missing.setAttribute('data-start', saved.process_start);
                        if (restoring) missing.setAttribute('data-restore', 'true');
                        field.append(missing);
                        message(text('记录的进程未运行或已变更，请重新选择', 'Saved process is missing or changed; select again'));
                    }
                    field.trigger('change.select2');
                });
            },
            error: function (_, status) {
                if (current === generation && status !== 'abort') message(text('读取进程失败，请重试', 'Process listing failed; retry'));
            },
            complete: function () {
                if (current === generation) { pending = null; controls(); }
            }
        });
        controls();
    }
    window.PerfCatIOSProcesses = {
        reset: reset,
        restore: restore,
        persist: persist,
        isLocked: function () { return locked; },
        acquire: function () {
            if (locked || pending) return false;
            var invalid = $('#ios-process-select, #ios-network-select').toArray().some(function (field) {
                var option = $(field).find(':selected');
                return option.attr('data-invalid') || (field.value && !option.attr('data-start'));
            });
            if (invalid) {
                message(text('请重新选择有效进程', 'Select a valid process'));
                return false;
            }
            snapshot = target();
            remember();
            locked = true;
            controls();
            return true;
        },
        release: function () { locked = false; snapshot = null; controls(); }
    };
    // The same immutable target must reach every metric, including device-wide graphics.
    $.ajaxPrefilter(function (options) {
        var path = new URL(options.url, window.location.href).pathname;
        if (!['/apm/cpu', '/apm/mem', '/apm/fps', '/apm/gpu', '/apm/network', '/apm/energy', '/apm/create/report'].includes(path)) return;
        var chosen = snapshot || target();
        var data = new URLSearchParams(options.data || '');
        data.set('process', chosen.process);
        data.set('process_start', chosen.process_start);
        data.set('network_process', chosen.network_process);
        data.set('network_process_start', chosen.network_process_start);
        options.data = data.toString();
    });
    $(function () {
        $('#ios-process-select, #ios-network-select').select2({width: '100%', theme: 'bootstrap-5', dropdownCssClass: 'ios-process-menu'});
        reset();
        $('.select-device').on('change.iosProcesses', reset);
        $('.select-app').on('change.iosProcesses', restore);
        $('#ios-process-select, #ios-network-select').on('change', function () { message(''); remember(); });
        $('#ios-process-refresh').on('click', refresh);
    });
}());

(function () {
    'use strict';
    var pending = null;
    var checkedDevices = new Set();
    var busyStates = ['creating', 'installing', 'verifying'];

    function text(cn, en) { return window.uiText(cn, en); }

    async function request(path, options) {
        var controller = new AbortController();
        var timeout = window.setTimeout(function () { controller.abort(); }, 45000);
        try {
            var response = await fetch(path, Object.assign({signal: controller.signal}, options));
            var data = await response.json();
            if (!response.ok || data.status !== 1) throw new Error(data.msg || response.statusText);
            return data;
        } finally {
            window.clearTimeout(timeout);
        }
    }

    function limitation(data) {
        return data.profile === 'legacy'
            ? text('用于 iOS 启动耗时工具；基础性能采集保持原有后端。', 'Used by iOS launch timing; basic sampling keeps its existing backend.')
            : text('新版采集使用 USB 隧道，需要开启开发者模式。首次采集可能下载并挂载开发者镜像。',
                   'Modern sampling uses a USB tunnel and requires Developer Mode. The first collection may download and mount a developer image.');
    }

    async function progress(profile) {
        var active = true;
        var timer;
        async function poll() {
            try {
                var data = await request('/ios/dependencies/status?profile=' + encodeURIComponent(profile));
                if (!active) return;
                var stage = {creating: text('创建本地环境', 'Creating local environment'),
                    installing: text('下载并安装依赖', 'Downloading and installing'),
                    verifying: text('验证依赖', 'Verifying dependencies'),
                    ready: text('依赖已安装', 'Dependencies installed'),
                    failed: text('安装失败', 'Installation failed')}[data.state] || data.state;
                var completed = !busyStates.includes(data.state);
                if (completed) {
                    Swal.hideLoading();
                    Swal.update({showConfirmButton: true, confirmButtonText: text('关闭', 'Close')});
                }
                // Swal.update rebuilds the HTML container; populate it afterwards.
                document.getElementById('ios-install-stage').textContent = stage;
                document.getElementById('ios-install-log').textContent = data.log || '';
                if (completed) {
                    document.getElementById('ios-install-note').textContent = data.error || limitation(data);
                    return;
                }
                timer = window.setTimeout(poll, 1500);
            } catch (error) {
                if (!active) return;
                Swal.hideLoading();
                Swal.update({showConfirmButton: true, confirmButtonText: text('关闭', 'Close')});
                document.getElementById('ios-install-note').textContent =
                    text('状态查询失败，可重新检测；后台安装可能仍在进行。', 'Status check failed. Check again; installation may still be running.') + ' ' + error.message;
            }
        }
        await Swal.fire({
            title: text('iOS 依赖安装', 'iOS Dependency Installation'),
            html: '<div id="ios-install-stage" role="status"></div>' +
                '<pre id="ios-install-log" style="max-height:240px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;text-align:left;font-size:12px"></pre>' +
                '<div id="ios-install-note" style="overflow-wrap:anywhere"></div>',
            allowOutsideClick: false, showConfirmButton: false, showCloseButton: true,
            didOpen: function () {
                document.getElementById('ios-install-note').textContent = text('关闭弹窗后安装仍会继续。', 'Installation continues if this dialog is closed.');
                Swal.showLoading();
                poll();
            },
            willClose: function () { active = false; window.clearTimeout(timer); }
        });
    }

    async function check(device, manual) {
        if (!device) {
            if (manual) await Swal.fire({icon: 'info', text: text('请先连接并选择 iOS 设备', 'Connect and select an iOS device first.')});
            return false;
        }
        if (!manual && checkedDevices.has(device)) return false;
        var data = await request('/ios/dependencies/check?device=' + encodeURIComponent(device));
        if ($('.select-device').val() !== device) return false;
        checkedDevices.add(device);
        if (busyStates.includes(data.state)) {
            await progress(data.profile);
            return false;
        }
        if (data.state === 'ready') {
            if (!data.collection_supported) {
                await Swal.fire({icon: 'info', title: text('依赖已就绪', 'Dependencies Ready'),
                    text: limitation(data), confirmButtonText: text('确定', 'OK')});
            }
            return data.collection_supported;
        }
        var specs = Object.keys(data.packages).map(function (name) { return name + '==' + data.packages[name]; });
        var result = await Swal.fire({
            icon: 'question', title: text('安装缺失的 iOS 依赖？', 'Install Missing iOS Dependencies?'),
            text: 'iOS ' + data.ios_version + '\n' + specs.join(', ') + '\n' +
                text('下载来源：', 'Source: ') + data.source + '\n' +
                text('本地环境：', 'Local environment: ') + data.environment + '\n' + limitation(data),
            showCancelButton: true, confirmButtonText: text('确认下载并安装', 'Download and Install'),
            cancelButtonText: text('暂不安装', 'Not Now'), allowOutsideClick: false,
            didOpen: function () {
                Object.assign(Swal.getHtmlContainer().style, {
                    whiteSpace: 'pre-line', overflowWrap: 'anywhere', textAlign: 'left', fontSize: '16px'
                });
            }
        });
        if (!result.isConfirmed) return data.profile === 'legacy';
        await request('/ios/dependencies/install', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-PerfCattle-Install-Token': data.token},
            body: JSON.stringify({confirmed: true, profile: data.profile})});
        await progress(data.profile);
        return false;
    }

    window.PerfCattleIOSDependencies = {
        isPending: function () { return pending !== null; },
        ensure: function (device, manual) {
            if (window.platform !== 'iOS') return Promise.resolve(true);
            if (pending) return Promise.resolve(false);
            pending = check(device, manual).catch(async function (error) {
                await Swal.fire({icon: 'error', title: text('依赖检测失败', 'Dependency Check Failed'),
                    text: error.message, confirmButtonText: text('确定', 'OK')});
                return false;
            }).finally(function () { pending = null; });
            return pending;
        }
    };
}());

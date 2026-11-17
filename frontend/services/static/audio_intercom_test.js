(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const root = $('dabomAudio');
    if (!root) return;
    const configuredRobotId = root.dataset.robotId || '';
    const connectButton = $('audioConnect');
    const listenButton = $('audioListen');
    const talkButton = $('audioTalk');
    const disconnectButton = $('audioDisconnect');
    const statusView = $('audioStatus');
    const logView = $('audioLog');
    const volumeSlider = $('audioListenVolume');
    const volumeLabel = $('audioListenVolumeLabel');
    const robotVolumeSlider = $('audioRobotVolume');
    const robotVolumeLabel = $('audioRobotVolumeLabel');
    const quickStatus = $('audioQuickState');
    let socket = null;
    let connectedRobot = false;
    let listening = false;
    let talking = false;
    let talkPress = 0;
    let restoreListeningAfterTalk = false;
    let microphoneStream = null;
    let microphoneContext = null;
    let playbackContext = null;
    let nextPlaybackTime = 0;
    let volume = 0.28; // Last confirmed preferred browser listening volume
    let speakerVolume = null;  // Must come from the Pi's actual ALSA mixer
    let speakerPending = false;
    let speakerTimer = null;
    let connecting = false;
    try {
        const saved = localStorage.getItem('dabomAudioListenVolume');
        if (saved !== null && /^\d{1,3}$/.test(saved) && +saved <= 100) {
            volumeSlider.value = saved;
        }
    } catch (_) { /* Storage is optional. */ }
    volume = Number(volumeSlider.value) / 100;
    volumeLabel.textContent = `${volumeSlider.value}%`;

    const info = message => {
        logView.textContent = `[${new Date().toLocaleTimeString('ko-KR')}] ${message}\n` + logView.textContent;
        logView.textContent = logView.textContent.slice(0, 3000);
    };
    const isConnected = () => socket?.readyState === WebSocket.OPEN;
    function show() {
        statusView.textContent = [
            `오디오 서버: ${isConnected() ? '연결됨' : '연결되지 않음'}`,
            `로봇 오디오: ${connectedRobot ? '연결됨' : '미연결'}`,
            `현장 청취: ${listening ? '켜짐' : '꺼짐'}`,
            `현장 송출: ${talking ? '진행 중' : '대기'}`
        ].join('\n');
        connectButton.disabled = connecting || Boolean(socket && socket.readyState < WebSocket.CLOSING);
        disconnectButton.disabled = !isConnected();
        listenButton.disabled = !isConnected() || !connectedRobot;
        talkButton.disabled = !isConnected() || !connectedRobot || !window.isSecureContext;
        listenButton.textContent = listening ? '현장 청취 중지' : '현장 청취 시작';
        listenButton.classList.toggle('active', listening);
        talkButton.classList.toggle('active', talking);
        if (quickStatus) quickStatus.textContent = isConnected()
            ? (connectedRobot ? '🟢 연결됨' : '🟡 로봇 대기') : '⚪ 연결 안 됨';
        if (robotVolumeSlider) {
            robotVolumeSlider.disabled = !isConnected() || !connectedRobot || speakerPending || speakerVolume === null;
            robotVolumeSlider.setAttribute('aria-busy', String(speakerPending));
            if (robotVolumeLabel && !speakerPending && speakerVolume === null) {
                robotVolumeLabel.textContent = '확인 중';
            }
        }
    }
    function sendControl(type, enabled) {
        if (isConnected()) socket.send(JSON.stringify({type, enabled}));
    }
    async function ensurePlayback() {
        if (!playbackContext || playbackContext.state === 'closed') {
            playbackContext = new AudioContext();
            nextPlaybackTime = 0;
        }
        await playbackContext.resume();
    }
    function playPacket(packet) {
        if (!listening || !playbackContext || playbackContext.state !== 'running' || packet.byteLength % 2) return;
        const view = new DataView(packet);
        const samples = view.byteLength / 2;
        if (!samples || samples > 2048) return;
        const buffer = playbackContext.createBuffer(1, samples, 16000);
        const channel = buffer.getChannelData(0);
        for (let i = 0; i < samples; i++) channel[i] = (view.getInt16(i * 2, true) / 32768) * volume;
        const source = playbackContext.createBufferSource();
        source.buffer = buffer;
        source.connect(playbackContext.destination);
        const now = playbackContext.currentTime;
        if (nextPlaybackTime < now || nextPlaybackTime > now + 0.35) nextPlaybackTime = now + 0.08;
        source.start(nextPlaybackTime);
        nextPlaybackTime += samples / 16000;
    }
    async function stopMicrophone() {
        talkPress++;
        if (talking) sendControl('talk', false);
        talking = false;
        if (restoreListeningAfterTalk && isConnected() && connectedRobot) {
            listening = true;
            sendControl('listen', true);
            nextPlaybackTime = 0;
        }
        restoreListeningAfterTalk = false;
        microphoneStream?.getTracks().forEach(track => track.stop());
        microphoneStream = null;
        if (microphoneContext) {
            const context = microphoneContext;
            microphoneContext = null;
            await context.close().catch(() => {});
        }
        show();
    }
    async function startMicrophone() {
        if (talking || !isConnected() || !connectedRobot) return;
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
            info('마이크 사용에는 HTTPS 또는 localhost의 보안 컨텍스트가 필요합니다.');
            return;
        }
        const request = ++talkPress;
        let stream;
        let context;
        try {
            stream = await navigator.mediaDevices.getUserMedia({audio: {
                echoCancellation: true, noiseSuppression: true, autoGainControl: true
            }, video: false});
            if (request !== talkPress || !isConnected()) { stream.getTracks().forEach(t => t.stop()); return; }
            context = new AudioContext();
            await context.audioWorklet.addModule('/static/audio_intercom_worklet.js?v=20261109-mvp1');
            await context.resume();
            if (request !== talkPress || !isConnected()) {
                stream.getTracks().forEach(t => t.stop());
                await context.close();
                return;
            }
            microphoneStream = stream;
            microphoneContext = context;
            const source = context.createMediaStreamSource(stream);
            const capture = new AudioWorkletNode(context, 'dabom-capture');
            const silentOutput = context.createGain();
            silentOutput.gain.value = 0;
            source.connect(capture).connect(silentOutput).connect(context.destination);
            capture.port.onmessage = event => {
                if (talking && isConnected() && socket.bufferedAmount < 32768) socket.send(event.data);
            };
            if (listening) {
                restoreListeningAfterTalk = true;
                listening = false;
                sendControl('listen', false); // avoid local acoustic feedback while PTT is held
            }
            talking = true;
            sendControl('talk', true);
            show();
        } catch (error) {
            stream?.getTracks().forEach(t => t.stop());
            if (context && context !== microphoneContext) await context.close().catch(() => {});
            info(`마이크 열기 실패: ${error.message}`);
        }
    }
    async function closeSocket() {
        await stopMicrophone();
        sendControl('listen', false);
        listening = false;
        socket?.close(1000, 'dashboard ended audio session');
        socket = null;
        connectedRobot = false;
        nextPlaybackTime = 0;
        await playbackContext?.close().catch(() => {});
        playbackContext = null;
        show();
    }
    async function resolveRobotId() {
        if (configuredRobotId) return configuredRobotId;
        const response = await fetch('/api/audio/status', { credentials: 'same-origin', cache: 'no-store' });
        if (!response.ok) throw new Error('오디오 상태 조회에 실패했습니다. 로그인 상태를 확인하세요.');
        const data = await response.json();
        if (!data.robot_id) throw new Error('로봇 ID가 없습니다.');
        return data.robot_id;
    }
    connectButton.addEventListener('click', async () => {
        if (connecting || isConnected()) return;
        connecting = true;
        show();
        let robotId;
        try { robotId = await resolveRobotId(); }
        catch (error) { connecting = false; info(`오디오 연결 준비 실패: ${error.message}`); show(); return; }
        connecting = false;
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        socket = new WebSocket(`${protocol}//${location.host}/ws/audio/dashboard/${encodeURIComponent(robotId)}`);
        socket.binaryType = 'arraybuffer';
        socket.onopen = () => { info('GPU 오디오 중계 연결 성공'); show(); };
        socket.onmessage = event => {
            if (event.data instanceof ArrayBuffer) { playPacket(event.data); return; }
            try {
                const message = JSON.parse(event.data);
                if (message.type === 'state') {
                    connectedRobot = Boolean(message.robot_connected);
                    if (!connectedRobot) { speakerVolume = null; clearSpeakerPending(); }
                    if (Number.isInteger(message.speaker_volume_percent)) {
                        applySpeakerVolume(message.speaker_volume_percent);
                    }
                    listening = Boolean(message.listening);
                    if (!connectedRobot) stopMicrophone();
                    show();
                } else if (message.type === 'speaker_volume') {
                    applySpeakerVolume(message.percent);
                } else if (message.type === 'error') {
                    clearSpeakerPending();
                    info(message.message);
                    if (robotVolumeSlider && speakerVolume !== null) robotVolumeSlider.value = speakerVolume;
                    if (robotVolumeLabel && speakerVolume !== null) robotVolumeLabel.textContent = `${speakerVolume}%`;
                    show();
                }
            } catch (error) { info(`오디오 상태 해석 실패: ${error.message}`); }
        };
        socket.onclose = event => {
            info(`오디오 연결 종료: ${event.code}`);
            socket = null;
            connectedRobot = false;
            listening = false;
            clearSpeakerPending();
            speakerVolume = null;
            stopMicrophone();
            show();
        };
        socket.onerror = () => info('오디오 네트워크 오류');
        show();
    });
    listenButton.addEventListener('click', async () => {
        if (!isConnected() || !connectedRobot) return;
        if (!listening) {
            try { await ensurePlayback(); }
            catch (error) { info(`출력 장치 오류: ${error.message}`); return; }
        }
        listening = !listening;
        if (!listening) nextPlaybackTime = 0;
        sendControl('listen', listening);
        show();
    });
    talkButton.addEventListener('pointerdown', event => {
        if (talkButton.disabled) return;
        event.preventDefault();
        talkButton.setPointerCapture(event.pointerId);
        startMicrophone();
    });
    talkButton.addEventListener('pointerup', stopMicrophone);
    talkButton.addEventListener('pointercancel', stopMicrophone);
    talkButton.addEventListener('lostpointercapture', stopMicrophone);
    talkButton.addEventListener('keydown', event => {
        if (event.code === 'Space' || event.code === 'Enter') {
            event.preventDefault();
            if (!event.repeat) startMicrophone();
        }
    });
    talkButton.addEventListener('keyup', event => {
        if (event.code === 'Space' || event.code === 'Enter') {
            event.preventDefault();
            stopMicrophone();
        }
    });
    disconnectButton.addEventListener('click', closeSocket);
    volumeSlider.addEventListener('input', () => {
        volume = Number(volumeSlider.value) / 100;
        volumeLabel.textContent = `${volumeSlider.value}%`;
        try { localStorage.setItem('dabomAudioListenVolume', volumeSlider.value); }
        catch (_) { /* Browser privacy mode may block storage. */ }
    });
    function clearSpeakerPending() {
        if (speakerTimer) clearTimeout(speakerTimer);
        speakerTimer = null;
        speakerPending = false;
    }
    function applySpeakerVolume(percent) {
        if (!Number.isInteger(percent) || percent < 0 || percent > 100) return;
        clearSpeakerPending();
        speakerVolume = percent;
        if (robotVolumeSlider) robotVolumeSlider.value = percent;
        if (robotVolumeLabel) robotVolumeLabel.textContent = `${percent}%`;
        show();
    }
    robotVolumeSlider?.addEventListener('input', () => {
        robotVolumeLabel.textContent = `${robotVolumeSlider.value}% (미적용)`;
    });
    robotVolumeSlider?.addEventListener('change', () => {
        if (!isConnected() || !connectedRobot || speakerVolume === null || speakerPending) return;
        const percent = Number(robotVolumeSlider.value);
        if (!Number.isInteger(percent) || percent < 0 || percent > 100) return;
        if (percent === speakerVolume) { applySpeakerVolume(speakerVolume); return; }
        speakerPending = true;
        robotVolumeLabel.textContent = `${percent}% (적용 중)`;
        socket.send(JSON.stringify({ type: 'set_speaker_volume', percent }));
        speakerTimer = setTimeout(() => {
            clearSpeakerPending();
            info('USB 스피커 음량 ACK 시간 초과: 실제 Pi 음량은 변경되었을 수 있습니다. 다시 연결하여 확인해 주세요.');
            speakerVolume = null; // Do not show an unverified setting.
            show();
        }, 3500);
        show();
    });
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) stopMicrophone();
    });
    window.addEventListener('pagehide', () => { closeSocket(); });
    if (!window.isSecureContext) {
        $('audioSecureNote').textContent = '현재 접속은 보안 컨텍스트가 아닙니다. 웹 마이크 송출은 localhost 또는 HTTPS 접속에서 사용하실 수 있습니다.';
    }
    show();
})();

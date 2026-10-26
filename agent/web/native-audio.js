export class NativeAudioSession {
  constructor({ controlToken, onEvent }) {
    this.controlToken = controlToken;
    this.onEvent = onEvent;
    this.sessionId = null;
    this.userId = null;
    this.voiceMode = 0;
    this.microphoneEnabled = false;
  }

  _emit(type, detail = {}) {
    this.onEvent({ type, ...detail });
  }

  async _request(path, body, { keepalive = false } = {}) {
    let response;
    try {
      response = await fetch(path, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Agent-Key": this.controlToken,
        },
        body: JSON.stringify(body),
        cache: "no-store",
        keepalive,
      });
    } catch (error) {
      throw new Error(`无法连接 Windows 音频助手：${String(error)}`);
    }

    let payload = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      try {
        payload = await response.json();
      } catch (_error) {
        payload = null;
      }
    }
    if (!response.ok) {
      throw new Error(payload?.detail || `HTTP ${response.status}`);
    }
    return payload;
  }

  async start(auth, voiceMode, comboVoice = null) {
    const status = await this._request("/api/native-audio/start", {
      token: auth.token,
      sessionId: auth.sessionId,
      voiceMode,
      comboVoice,
    });
    this.sessionId = auth.sessionId;
    this.userId = status.userId || auth.userId;
    this.voiceMode = Number(status.voiceMode || 0);
    this.microphoneEnabled = false;
    this._emit("connected", {
      userId: this.userId,
      voiceMode: this.voiceMode,
    });
    return status;
  }

  async setMicrophone(enabled) {
    const status = await this._request("/api/native-audio/microphone", {
      enabled: Boolean(enabled),
    });
    this.microphoneEnabled = Boolean(status.microphoneEnabled);
    this._emit("microphone", { enabled: this.microphoneEnabled });
    return status;
  }

  async setVoiceMode(mode, comboVoice = null) {
    const status = await this._request("/api/native-audio/voice", {
      mode: Number(mode),
      comboVoice,
    });
    this.voiceMode = Number(status.voiceMode);
    this._emit("voice", { mode: this.voiceMode });
    return status;
  }

  async refreshToken(token) {
    const status = await this._request("/api/native-audio/token", { token });
    this._emit("token-refreshed");
    return status;
  }

  async heartbeat() {
    return this._request("/api/native-audio/heartbeat", {});
  }

  async stop({ keepalive = false } = {}) {
    if (!this.sessionId) {
      return;
    }
    try {
      await this._request(
        "/api/native-audio/stop",
        {},
        { keepalive },
      );
    } finally {
      this.sessionId = null;
      this.userId = null;
      this.microphoneEnabled = false;
      this._emit("disconnected");
    }
  }
}

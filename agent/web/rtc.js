const CONNECTION_STATUS = {
  0: "初始化",
  1: "已断开",
  2: "连接中",
  3: "已连接",
  4: "重连中",
  5: "连接失败",
};

const SUBSCRIBE_STATE = {
  0: "初始化",
  1: "未订阅",
  2: "订阅中",
  3: "已订阅",
};


export class AgentRtcSession {
  constructor({ videoElement, onEvent }) {
    this.videoElement = videoElement;
    this.onEvent = onEvent;
    this.engine = null;
    this.currentAuth = null;
    this.remoteUserIds = new Set();
    this.videoUserId = null;
    this.microphoneEnabled = false;
    this.joined = false;
  }

  _emit(type, detail = {}) {
    this.onEvent({ type, ...detail });
  }

  _engineClass() {
    const sdk = window.AliRtcEngine;
    if (!sdk) {
      throw new Error("ARTC Web SDK 未加载");
    }
    return sdk.default || sdk;
  }

  async connect(auth) {
    if (this.engine) {
      throw new Error("已有客服媒体连接");
    }

    const Engine = this._engineClass();
    const engine = new Engine();
    this.engine = engine;
    this.currentAuth = auth;
    this._bindEngineEvents(engine);

    try {
      // 入会前关闭发布，SDK 不会自动采集客服麦克风或摄像头。
      await engine.publishLocalAudioStream(false);
      await engine.publishLocalVideoStream(false);
      await engine.joinChannel(auth.token, auth.userName || auth.userId);

      this.joined = true;
      // 再次同步状态，防止版本差异导致 close 调用前恢复默认发布。
      await engine.publishLocalAudioStream(false);
      await engine.publishLocalVideoStream(false);

      this._scanRemoteUsers();
      this._emit("connected", {
        channel: auth.channel,
        userId: auth.userId,
      });
    } catch (error) {
      await this.disconnect({ emit: false });
      throw error;
    }
  }

  _bindEngineEvents(engine) {
    engine.on("connectionStatusChange", (state, reason) => {
      this._emit("connection", {
        state,
        label: CONNECTION_STATUS[state] || `状态 ${state}`,
        reason: String(reason || ""),
      });
    });

    engine.on("remoteUserOnLineNotify", (userId) => {
      this.remoteUserIds.add(userId);
      this._emit("remote-user", { userId, online: true });
    });

    engine.on("remoteUserOffLineNotify", (userId) => {
      this.remoteUserIds.delete(userId);
      if (this.videoUserId === userId) {
        this._clearRemoteVideo();
      }
      this._emit("remote-user", { userId, online: false });
    });

    engine.on("audioSubscribeStateChanged", (userId, oldState, newState) => {
      this._emit("audio-subscription", {
        userId,
        oldState,
        newState,
        label: SUBSCRIBE_STATE[newState] || `状态 ${newState}`,
      });
    });

    engine.on("videoSubscribeStateChanged", (userId, oldState, newState) => {
      if (newState === 3) {
        this._showRemoteVideo(userId);
      } else if (newState === 1 && this.videoUserId === userId) {
        this._clearRemoteVideo();
      }
      this._emit("video-subscription", {
        userId,
        oldState,
        newState,
        label: SUBSCRIBE_STATE[newState] || `状态 ${newState}`,
      });
    });

    engine.on("remoteAudioAutoPlayFail", (userId) => {
      this._emit("audio-autoplay-blocked", { userId });
    });

    engine.on("authInfoWillExpire", () => {
      this._emit("token-expiring");
    });

    engine.on("authInfoExpired", () => {
      this._emit("token-expired");
    });

    engine.on("onOccurError", (code, message) => {
      this._emit("error", {
        code: String(code || ""),
        message: String(message || "ARTC 媒体异常"),
      });
    });
  }

  _scanRemoteUsers() {
    if (!this.engine?.remoteUsers) {
      return;
    }
    for (const user of this.engine.remoteUsers) {
      const userId = user.userId || user.userID;
      if (userId) {
        this.remoteUserIds.add(userId);
      }
      if (userId && user.hasCamera) {
        this._showRemoteVideo(userId);
      }
    }
  }

  _showRemoteVideo(userId) {
    if (!this.engine || !userId) {
      return;
    }
    this.videoUserId = userId;
    this.videoElement.hidden = false;
    this.engine.setRemoteViewConfig(this.videoElement, userId, 1);
    this._emit("remote-video", { active: true, userId });
  }

  _clearRemoteVideo() {
    if (this.engine && this.videoUserId) {
      try {
        this.engine.setRemoteViewConfig(null, this.videoUserId, 1);
      } catch (_error) {
        // 离会过程中 SDK 可能已经清理远端视图。
      }
    }
    this.videoUserId = null;
    this.videoElement.hidden = true;
    this.videoElement.srcObject = null;
    this._emit("remote-video", { active: false, userId: null });
  }

  async setMicrophone(enabled) {
    if (!this.engine || !this.joined) {
      if (enabled) {
        throw new Error("媒体会话未接入");
      }
      return;
    }
    await this.engine.publishLocalAudioStream(Boolean(enabled));
    this.microphoneEnabled = Boolean(enabled);
    this._emit("microphone", { enabled: this.microphoneEnabled });
  }

  async refreshToken(auth) {
    if (!this.engine || !this.joined) {
      throw new Error("媒体会话未接入");
    }
    await this.engine.refreshAuthInfo(auth.token);
    this.currentAuth = auth;
    this._emit("token-refreshed", { expiresAt: auth.expiresAt });
  }

  async resumeRemoteAudio() {
    if (!this.engine || !this.joined) {
      return;
    }
    const userId = Array.from(this.remoteUserIds)[0];
    if (userId) {
      try {
        this.engine.resumeRemoteAudioStream(userId);
      } catch (_error) {
        // 部分 SDK 版本仅允许通过全局播放控制恢复。
      }
    }
    if (typeof this.engine.muteAllRemoteAudioPlaying === "function") {
      this.engine.muteAllRemoteAudioPlaying(false);
    }
    this._emit("audio-resume-requested");
  }

  async disconnect({ emit = true } = {}) {
    const engine = this.engine;
    if (!engine) {
      return;
    }

    this.joined = false;
    try {
      await engine.publishLocalAudioStream(false);
    } catch (_error) {
      // 连接已损坏时继续清理其余资源。
    }
    this._clearRemoteVideo();

    try {
      await engine.leaveChannel();
    } catch (_error) {
      // destroy 会再次尝试释放媒体资源。
    }
    try {
      await engine.destroy();
    } catch (_error) {
      // SDK 已释放或实例失效时不再阻塞工作台。
    }

    this.engine = null;
    this.currentAuth = null;
    this.remoteUserIds.clear();
    this.microphoneEnabled = false;
    if (emit) {
      this._emit("disconnected");
    }
  }
}

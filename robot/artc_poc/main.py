from __future__ import annotations

import argparse
import asyncio
import base64
import json
import select
import stat
import sys
import time
from pathlib import Path

from .audio import create_pcm_output
from .engine import (
    OfficialArtcEngine,
    OfficialSdkError,
    resolve_official_sdk_paths,
)
from .microphone import MicrophonePublisher
from .session import PoCSessionError, RobotPoCSession
from .tone import TonePublisher


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ARTC Linux Python 机器人 PoC（默认关闭麦克风）"
    )
    parser.add_argument(
        "--sdk-root",
        type=Path,
        required=True,
        help="解压后的 ARTC Linux SDK 根目录或其 Python 目录",
    )
    parser.add_argument(
        "--core-service",
        type=Path,
        help="可选 AliRtcCoreService 路径；默认从 SDK 目录推导",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        required=True,
        help="只包含单参数 ARTC Token 的短期文件",
    )
    parser.add_argument("--channel", required=True, help="ARTC 频道 ID")
    parser.add_argument("--user-id", required=True, help="机器人 ARTC userId")
    parser.add_argument("--user-name", help="机器人显示名，默认使用 userId")
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("/tmp/artc-robot-logs"),
        help="ARTC SDK 日志目录",
    )
    parser.add_argument(
        "--join-timeout",
        type=float,
        default=20,
        help="等待入会回调的秒数",
    )
    parser.add_argument(
        "--run-seconds",
        type=float,
        help="入会后自动退出前的运行秒数；无 TTY 时必须设置",
    )
    parser.add_argument(
        "--no-audio-output",
        action="store_true",
        help="不播放远端 PCM，仅统计接收帧；用于无声卡环境",
    )
    parser.add_argument(
        "--microphone-on",
        action="store_true",
        help="入会后自动打开麦克风；默认保持关闭",
    )
    parser.add_argument(
        "--test-tone-frequency",
        type=float,
        help="测试用合成 PCM 音调频率；不读取真实麦克风",
    )
    parser.add_argument(
        "--test-tone-amplitude",
        type=float,
        default=0.2,
        help="测试音幅度，范围 (0, 1]，默认 0.2",
    )
    return parser.parse_args()


def pump_events(seconds: float) -> None:
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.sleep(seconds))


def read_token(path: Path) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PoCSessionError(
                f"Token 文件权限过宽：{path} 为 {mode:o}，应为 600"
            )
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PoCSessionError(f"无法读取 Token 文件 {path}: {exc}") from exc
    if not token:
        raise PoCSessionError(f"Token 文件为空：{path}")
    return token


def validate_token_scope(token: str, channel: str, user_id: str) -> None:
    try:
        payload = json.loads(base64.b64decode(token, validate=True))
    except (ValueError, json.JSONDecodeError) as exc:
        raise PoCSessionError("Token 不是有效的 ARTC 单参数 Base64 Token") from exc
    if not isinstance(payload, dict):
        raise PoCSessionError("Token 解码后不是 JSON 对象")
    if payload.get("channelid") != channel:
        raise PoCSessionError("Token 的 ChannelID 与 --channel 不一致")
    if payload.get("userid") != user_id:
        raise PoCSessionError("Token 的 UserId 与 --user-id 不一致")
    expires_at = payload.get("timestamp")
    if not isinstance(expires_at, int):
        raise PoCSessionError("Token 缺少有效的 timestamp")
    if expires_at <= int(time.time()):
        raise PoCSessionError("Token 已过期")


def wait_for_join(session: RobotPoCSession, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while (
        not session.join_finished.is_set()
        and time.monotonic() < deadline
    ):
        pump_events(0.05)
    if not session.join_finished.is_set():
        raise PoCSessionError(f"等待 ARTC 入会回调超时：{timeout_seconds} 秒")
    if session.state != "joined":
        raise PoCSessionError(session.error_message or "ARTC 入会失败")


def print_status(session: RobotPoCSession) -> None:
    remote_users = ",".join(sorted(session.remote_users)) or "-"
    print(
        f"状态={session.state} 麦克风="
        f"{'开启' if session.microphone_enabled else '关闭'} "
        f"远端用户={remote_users} 音频帧={session.remote_audio_frames} "
        f"音频字节={session.remote_audio_bytes} "
        f"峰值={session.remote_audio_peak} "
        f"最近错误={session.last_error or '-'}",
        flush=True,
    )


def set_microphone_enabled(
    session: RobotPoCSession,
    audio_publisher: TonePublisher | MicrophonePublisher | None,
    enabled: bool,
) -> None:
    if enabled:
        if audio_publisher is not None:
            try:
                audio_publisher.start()
                audio_publisher.prime(5)
            except Exception:
                audio_publisher.stop()
                raise
            if audio_publisher.last_error:
                audio_publisher.stop()
                raise PoCSessionError(
                    f"audio source failed: {audio_publisher.last_error}"
                )
        try:
            session.set_microphone_enabled(True)
        except Exception:
            if audio_publisher is not None:
                audio_publisher.stop()
            raise
        return

    if audio_publisher is not None:
        audio_publisher.stop()
    session.set_microphone_enabled(False)


def run_command_loop(
    session: RobotPoCSession,
    audio_publisher: TonePublisher | MicrophonePublisher | None,
    run_seconds: float | None,
) -> None:
    print(
        "命令：mic on | mic off | status | quit",
        flush=True,
    )
    if run_seconds is not None:
        deadline = time.monotonic() + run_seconds
    else:
        if not sys.stdin.isatty():
            raise PoCSessionError(
                "当前标准输入不是 TTY；无交互运行请设置 --run-seconds"
            )
        deadline = None

    while session.state == "joined":
        if audio_publisher is not None:
            audio_publisher.pump()
        if deadline is not None and time.monotonic() >= deadline:
            break
        timeout = 0.01
        if deadline is not None:
            timeout = min(timeout, max(0.0, deadline - time.monotonic()))
        if sys.stdin.isatty():
            readable, _, _ = select.select([sys.stdin], [], [], timeout)
            if not readable:
                pump_events(0.01)
                continue
            command = sys.stdin.readline().strip().lower()
        else:
            pump_events(timeout)
            if audio_publisher is not None:
                audio_publisher.pump()
            continue

        if command == "quit":
            break
        if command == "status":
            print_status(session)
        elif command in {"mic on", "mic off"}:
            try:
                set_microphone_enabled(
                    session,
                    audio_publisher,
                    command == "mic on",
                )
            except PoCSessionError as exc:
                print(f"操作失败：{exc}", file=sys.stderr, flush=True)
            else:
                print_status(session)
        elif command:
            print("未知命令；可用：mic on | mic off | status | quit", flush=True)
        pump_events(0.01)
        if audio_publisher is not None:
            audio_publisher.pump()


def main() -> int:
    args = parse_args()
    session: RobotPoCSession | None = None
    audio_publisher: TonePublisher | MicrophonePublisher | None = None
    try:
        paths = resolve_official_sdk_paths(args.sdk_root, args.core_service)
        token = read_token(args.token_file)
        validate_token_scope(token, args.channel, args.user_id)
        audio_output = create_pcm_output(not args.no_audio_output)
        session = RobotPoCSession(audio_output)

        asyncio.set_event_loop(asyncio.new_event_loop())
        engine = OfficialArtcEngine.create(
            paths,
            session,
            log_dir=args.log_dir,
        )
        if args.test_tone_frequency is not None:
            audio_publisher = TonePublisher(
                engine,
                sample_rate=16000,
                frequency_hz=args.test_tone_frequency,
                amplitude=args.test_tone_amplitude,
            )
        else:
            audio_publisher = MicrophonePublisher(
                engine,
                sample_rate=16000,
                channels=1,
            )
        session.attach_engine(engine)
        session.join(
            token,
            args.channel,
            args.user_id,
            args.user_name or args.user_id,
        )
        wait_for_join(session, args.join_timeout)
        result = engine.enable_external_audio_source(16000, 1)
        if result != 0:
            raise PoCSessionError(
                f"Failed to enable external audio source: {result}"
            )
        print(
            f"已加入频道 {args.channel}；机器人麦克风默认关闭，"
            f"userId={args.user_id}",
            flush=True,
        )
        if args.microphone_on:
            set_microphone_enabled(session, audio_publisher, True)
            print_status(session)
        run_command_loop(session, audio_publisher, args.run_seconds)
        return 0
    except (
        OfficialSdkError,
        PoCSessionError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"ARTC PoC 失败：{exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("收到中断，正在停止 ARTC PoC。", flush=True)
        return 0
    finally:
        if audio_publisher is not None:
            audio_publisher.stop()
        if session is not None:
            session.close(pump_events)
            print_status(session)
            if isinstance(audio_publisher, TonePublisher):
                print(
                    f"测试音输入帧={audio_publisher.pushed_frames} "
                    f"错误={audio_publisher.push_errors} "
                    f"最后结果={audio_publisher.last_result} "
                    f"错误信息={audio_publisher.last_error or '-'}",
                    flush=True,
                )
            elif isinstance(audio_publisher, MicrophonePublisher):
                print(
                    f"麦克风输入帧={audio_publisher.pushed_frames} "
                    f"错误={audio_publisher.push_errors} "
                    f"丢弃={audio_publisher.dropped_frames} "
                    f"错误信息={audio_publisher.last_error or '-'}",
                    flush=True,
                )


if __name__ == "__main__":
    raise SystemExit(main())

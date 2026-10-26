#include <windows.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "rtc/engine_interface.h"

namespace {

std::mutex g_output_mutex;

std::string sanitize_field(const char* value) {
    if (value == nullptr) {
        return "-";
    }
    std::string result(value);
    for (char& character : result) {
        if (character == '\t' || character == '\r' || character == '\n') {
            character = ' ';
        }
    }
    return result;
}

void emit(const std::string& line) {
    std::lock_guard<std::mutex> lock(g_output_mutex);
    std::cout << line << std::endl;
}

std::vector<std::string> split_tabs(const std::string& line) {
    std::vector<std::string> fields;
    std::istringstream input(line);
    std::string field;
    while (std::getline(input, field, '\t')) {
        fields.push_back(field);
    }
    return fields;
}

class EngineEvents final : public AliRTCSdk::AliEngineEventListener {
public:
    void OnJoinChannelResult(
        int result,
        const char* channel,
        const char* userId,
        int elapsed
    ) override {
        if (result == 0) {
            joined_.store(true);
        }
        emit(
            "JOINED\t" + std::to_string(result) + "\t" +
            sanitize_field(channel) + "\t" + sanitize_field(userId) + "\t" +
            std::to_string(elapsed)
        );
    }

    void OnLeaveChannelResult(
        int result,
        AliRTCSdk::AliEngineStats stats
    ) override {
        joined_.store(false);
        left_.store(true);
        emit(
            "LEFT\t" + std::to_string(result) + "\t" +
            std::to_string(stats.callDuration)
        );
    }

    void OnConnectionStatusChange(int status, int reason) override {
        emit(
            "CONNECTION\t" + std::to_string(status) + "\t" +
            std::to_string(reason)
        );
    }

    void OnOccurError(int error, const char* message) override {
        (void)message;
        emit("ERROR\t" + std::to_string(error));
    }

    void OnBye(int code) override {
        joined_.store(false);
        emit("BYE\t" + std::to_string(code));
    }

    bool joined() const {
        return joined_.load();
    }

    void reset_left() {
        left_.store(false);
    }

    bool wait_left(int timeout_ms) const {
        const auto deadline =
            std::chrono::steady_clock::now() +
            std::chrono::milliseconds(timeout_ms);
        while (std::chrono::steady_clock::now() < deadline) {
            if (left_.load()) {
                return true;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
        }
        return left_.load();
    }

private:
    std::atomic<bool> joined_{false};
    std::atomic<bool> left_{true};
};

bool parse_voice_mode(
    const std::string& raw,
    AliRTCSdk::AliEngineAudioEffectVoiceChangerMode* mode
) {
    if (raw.empty()) {
        return false;
    }
    char* end = nullptr;
    const long value = std::strtol(raw.c_str(), &end, 10);
    if (
        end == raw.c_str() ||
        *end != '\0' ||
        value < 0 ||
        value > static_cast<long>(
            AliRTCSdk::AliEngineAudioEffectVoiceChangerPhonograph
        )
    ) {
        return false;
    }
    *mode = static_cast<AliRTCSdk::AliEngineAudioEffectVoiceChangerMode>(
        value
    );
    return true;
}

int reset_audio_effects(
    AliRTCSdk::AliEngine* engine,
    const char** failed_step
) {
    *failed_step = "reset_pitch";
    int result = engine->SetAudioEffectPitchValue(1.0);
    if (result != 0) {
        return result;
    }
    *failed_step = "reset_reverb";
    result = engine->SetAudioEffectReverbMode(
        AliRTCSdk::AliEngineAudioEffectReverbOff
    );
    if (result != 0) {
        return result;
    }
    *failed_step = "reset_beautify";
    result = engine->SetAudioEffectBeautifyMode(
        AliRTCSdk::AliEngineAudioEffectBeautifyOff
    );
    if (result != 0) {
        return result;
    }
    *failed_step = "reset_equalizer";
    for (int band = 0; band <= 9; ++band) {
        result = engine->SetAudioEffectEqualizationParam(
            static_cast<
                AliRTCSdk::AliEngineAudioEffectEqualizationBandFrequency
            >(band),
            0.0f
        );
        if (result != 0) {
            return result;
        }
    }
    return 0;
}

int apply_preset_voice(
    AliRTCSdk::AliEngine* engine,
    AliRTCSdk::AliEngineAudioEffectVoiceChangerMode mode,
    const char** failed_step
) {
    int result = reset_audio_effects(engine, failed_step);
    if (result != 0) {
        return result;
    }
    *failed_step = "voice_changer";
    return engine->SetAudioEffectVoiceChangerMode(mode);
}

struct CombinationVoiceSettings {
    AliRTCSdk::AliEngineAudioEffectVoiceChangerMode voice_mode;
    double pitch;
    AliRTCSdk::AliEngineAudioEffectReverbMode reverb_mode;
    AliRTCSdk::AliEngineAudioEffectBeautifyMode beautify_mode;
    float low_gain;
    float mid_gain;
    float high_gain;
};

bool parse_double_in_range(
    const std::string& raw,
    double minimum,
    double maximum,
    double* value
) {
    if (raw.empty()) {
        return false;
    }
    char* end = nullptr;
    const double parsed = std::strtod(raw.c_str(), &end);
    if (
        end == raw.c_str() ||
        *end != '\0' ||
        !std::isfinite(parsed) ||
        parsed < minimum ||
        parsed > maximum
    ) {
        return false;
    }
    *value = parsed;
    return true;
}

bool parse_integer_in_range(
    const std::string& raw,
    int minimum,
    int maximum,
    int* value
) {
    if (raw.empty()) {
        return false;
    }
    char* end = nullptr;
    const long parsed = std::strtol(raw.c_str(), &end, 10);
    if (
        end == raw.c_str() ||
        *end != '\0' ||
        parsed < minimum ||
        parsed > maximum
    ) {
        return false;
    }
    *value = static_cast<int>(parsed);
    return true;
}

bool parse_combination_voice(
    const std::vector<std::string>& fields,
    CombinationVoiceSettings* settings
) {
    int voice_mode = 0;
    int reverb_mode = 0;
    int beautify_mode = 0;
    double pitch = 1.0;
    double low_gain = 0.0;
    double mid_gain = 0.0;
    double high_gain = 0.0;
    if (
        fields.size() != 8 ||
        !parse_integer_in_range(fields[1], 0, 11, &voice_mode) ||
        !parse_double_in_range(fields[2], 0.5, 2.0, &pitch) ||
        !parse_integer_in_range(fields[3], 0, 8, &reverb_mode) ||
        !parse_integer_in_range(fields[4], 0, 2, &beautify_mode) ||
        !parse_double_in_range(fields[5], -15.0, 15.0, &low_gain) ||
        !parse_double_in_range(fields[6], -15.0, 15.0, &mid_gain) ||
        !parse_double_in_range(fields[7], -15.0, 15.0, &high_gain)
    ) {
        return false;
    }
    settings->voice_mode = static_cast<
        AliRTCSdk::AliEngineAudioEffectVoiceChangerMode
    >(voice_mode);
    settings->pitch = pitch;
    settings->reverb_mode = static_cast<
        AliRTCSdk::AliEngineAudioEffectReverbMode
    >(reverb_mode);
    settings->beautify_mode = static_cast<
        AliRTCSdk::AliEngineAudioEffectBeautifyMode
    >(beautify_mode);
    settings->low_gain = static_cast<float>(low_gain);
    settings->mid_gain = static_cast<float>(mid_gain);
    settings->high_gain = static_cast<float>(high_gain);
    return true;
}

int apply_combination_voice(
    AliRTCSdk::AliEngine* engine,
    const CombinationVoiceSettings& settings,
    const char** failed_step
) {
    int result = reset_audio_effects(engine, failed_step);
    if (result != 0) {
        return result;
    }
    *failed_step = "combination_voice_changer";
    result = engine->SetAudioEffectVoiceChangerMode(settings.voice_mode);
    if (result != 0) {
        return result;
    }
    *failed_step = "combination_pitch";
    result = engine->SetAudioEffectPitchValue(settings.pitch);
    if (result != 0) {
        return result;
    }
    *failed_step = "combination_reverb";
    result = engine->SetAudioEffectReverbMode(settings.reverb_mode);
    if (result != 0) {
        return result;
    }
    *failed_step = "combination_beautify";
    result = engine->SetAudioEffectBeautifyMode(settings.beautify_mode);
    if (result != 0) {
        return result;
    }
    *failed_step = "combination_low_gain";
    result = engine->SetAudioEffectEqualizationParam(
        AliRTCSdk::AliRtcAudioEffectEqualizationBand250,
        settings.low_gain
    );
    if (result != 0) {
        return result;
    }
    *failed_step = "combination_mid_gain";
    result = engine->SetAudioEffectEqualizationParam(
        AliRTCSdk::AliRtcAudioEffectEqualizationBand1K,
        settings.mid_gain
    );
    if (result != 0) {
        return result;
    }
    *failed_step = "combination_high_gain";
    return engine->SetAudioEffectEqualizationParam(
        AliRTCSdk::AliRtcAudioEffectEqualizationBand4K,
        settings.high_gain
    );
}

int configure_engine(
    AliRTCSdk::AliEngine* engine,
    const char** failed_step
) {
    *failed_step = "audio_only";
    int result = engine->SetAudioOnlyMode(true);
    if (result != 0) {
        return result;
    }
    *failed_step = "default_audio_subscribe";
    result = engine->SetDefaultSubscribeAllRemoteAudioStreams(false);
    if (result != 0) {
        return result;
    }
    *failed_step = "all_audio_subscribe";
    result = engine->SubscribeAllRemoteAudioStreams(false);
    if (result != 0) {
        return result;
    }
    *failed_step = "publish_audio_off";
    return engine->PublishLocalAudioStream(false);
}

}  // namespace

int main() {
    const HRESULT com_result =
        CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    const bool should_uninitialize_com = SUCCEEDED(com_result);

    AliRTCSdk::AliEngine* engine =
        AliRTCSdk::AliEngine::Create("");
    if (engine == nullptr) {
        emit("FATAL\tengine_create_failed");
        if (should_uninitialize_com) {
            CoUninitialize();
        }
        return 2;
    }

    EngineEvents events;
    if (engine->SetEngineEventListener(&events) != 0) {
        emit("FATAL\tlistener_registration_failed");
        engine->SetEngineEventListener(nullptr);
        AliRTCSdk::AliEngine::Destroy();
        if (should_uninitialize_com) {
            CoUninitialize();
        }
        return 3;
    }

    const char* configure_step = "unknown";
    const int configure_result = configure_engine(engine, &configure_step);
    if (configure_result != 0) {
        emit(
            "FATAL\tconfigure_failed\t" + std::string(configure_step) + "\t" +
            std::to_string(configure_result)
        );
        engine->SetEngineEventListener(nullptr);
        AliRTCSdk::AliEngine::Destroy();
        if (should_uninitialize_com) {
            CoUninitialize();
        }
        return 4;
    }

    emit("READY");

    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) {
            continue;
        }
        const std::vector<std::string> fields = split_tabs(line);
        const std::string& command = fields[0];

        if (command == "JOIN") {
            if (fields.size() != 2 || fields[1].empty()) {
                emit("COMMAND_ERROR\tJOIN_INVALID");
                continue;
            }
            if (events.joined() || engine->IsInCall()) {
                emit("JOIN_REQUEST\t1");
                continue;
            }
            const int result = engine->JoinChannel(
                fields[1].c_str(),
                nullptr,
                nullptr,
                "AgentVoice"
            );
            emit("JOIN_REQUEST\t" + std::to_string(result));
            continue;
        }

        if (command == "MIC") {
            if (fields.size() != 2 || (fields[1] != "0" && fields[1] != "1")) {
                emit("COMMAND_ERROR\tMIC_INVALID");
                continue;
            }
            const bool enabled = fields[1] == "1";
            if (!events.joined()) {
                emit("MIC_RESULT\t1\t" + std::to_string(enabled ? 1 : 0));
                continue;
            }
            const int result = engine->PublishLocalAudioStream(enabled);
            emit(
                "MIC_RESULT\t" + std::to_string(result) + "\t" +
                std::to_string(enabled ? 1 : 0)
            );
            continue;
        }

        if (command == "VOICE") {
            AliRTCSdk::AliEngineAudioEffectVoiceChangerMode mode =
                AliRTCSdk::AliEngineAudioEffectVoiceChangerOff;
            if (
                fields.size() != 2 ||
                !parse_voice_mode(fields[1], &mode)
            ) {
                emit("COMMAND_ERROR\tVOICE_INVALID");
                continue;
            }
            const char* failed_step = "unknown";
            const int result = apply_preset_voice(
                engine,
                mode,
                &failed_step
            );
            emit(
                "VOICE_RESULT\t" + std::to_string(result) + "\t" +
                fields[1] + "\t" + failed_step
            );
            continue;
        }

        if (command == "COMBO") {
            CombinationVoiceSettings settings{};
            if (!parse_combination_voice(fields, &settings)) {
                emit("COMMAND_ERROR\tCOMBO_INVALID");
                continue;
            }
            const char* failed_step = "unknown";
            const int result = apply_combination_voice(
                engine,
                settings,
                &failed_step
            );
            emit(
                "COMBO_RESULT\t" + std::to_string(result) + "\t" +
                fields[1] + "\t" + fields[2] + "\t" + fields[3] + "\t" +
                fields[4] + "\t" + fields[5] + "\t" + fields[6] + "\t" +
                fields[7] + "\t" + failed_step
            );
            continue;
        }

        if (command == "TOKEN") {
            if (fields.size() != 2 || fields[1].empty()) {
                emit("COMMAND_ERROR\tTOKEN_INVALID");
                continue;
            }
            const int result = engine->RefreshAuthInfo(fields[1].c_str());
            emit("TOKEN_RESULT\t" + std::to_string(result));
            continue;
        }

        if (command == "STOP") {
            break;
        }

        emit("COMMAND_ERROR\tUNKNOWN");
    }

    engine->PublishLocalAudioStream(false);
    if (engine->IsInCall()) {
        events.reset_left();
        engine->LeaveChannel();
        events.wait_left(3000);
    }
    engine->SetEngineEventListener(nullptr);
    AliRTCSdk::AliEngine::Destroy();
    if (should_uninitialize_com) {
        CoUninitialize();
    }
    emit("STOPPED");
    return 0;
}

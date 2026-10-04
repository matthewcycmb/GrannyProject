// GOOUUU ESP32-S3-CAM V1.5, OV2640, 16 MB flash / 8 MB octal PSRAM.
// Camera pins verified against Documents/ESP32S3CAM-Pin.jpg in:
// https://github.com/zhuhai-esp/ESP32-S3-Goouuu-Cam
// Stream framing follows Espressif's CameraWebServer example.
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <atomic>
#include "lwip/sockets.h"
#include "esp_camera.h"
#include "esp_http_server.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
// A credential-free build can be compiled, but never opens a camera/network.
static const char WIFI_SSID[] = "";
static const char WIFI_PASSWORD[] = "";
#endif

static httpd_handle_t statusServer = nullptr;
static httpd_handle_t streamServer = nullptr;
static const char BOUNDARY[] = "\r\n--123456789000000000000987654321\r\n";
static std::atomic<uint32_t> framesSent{0}, captureMs{0}, sendMs{0};
static std::atomic<uint32_t> snapshotsSent{0};
static WiFiUDP videoUdp;
static std::atomic<uint32_t> udpFramesSent{0};
static constexpr int JPEG_QUALITY = 24;
static constexpr unsigned long FRAME_INTERVAL_MS = 200; // At most 5 FPS; limit hotspot load.

static void halt(const char *message) {
    Serial.println(message);
    while (true) delay(1000);
}

static esp_err_t stream(httpd_req_t *request) {
    // Multipart boundaries and headers are small writes. Send them immediately
    // instead of waiting for TCP to combine them with later JPEG data.
    int noDelay = 1;
    if (setsockopt(httpd_req_to_sockfd(request), IPPROTO_TCP, TCP_NODELAY,
                   &noDelay, sizeof(noDelay)) != 0) {
        Serial.println("Camera stream socket configuration failed.");
        return ESP_FAIL;
    }
    httpd_resp_set_type(request,
        "multipart/x-mixed-replace;boundary=123456789000000000000987654321");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    while (WiFi.status() == WL_CONNECTED) {
        unsigned long captureStarted = millis();
        camera_fb_t *frame = esp_camera_fb_get();
        captureMs.store(millis() - captureStarted, std::memory_order_relaxed);
        if (!frame) {
            Serial.println("Camera capture failed; closing stream.");
            return ESP_FAIL;
        }
        if (frame->format != PIXFORMAT_JPEG || frame->len < 4) {
            esp_camera_fb_return(frame);
            return ESP_FAIL;
        }
        char header[128];
        int length = snprintf(header, sizeof(header),
            "Content-Type: image/jpeg\r\nContent-Length: %u\r\n"
            "X-Timestamp: %ld.%06ld\r\n\r\n",
            static_cast<unsigned>(frame->len),
            static_cast<long>(frame->timestamp.tv_sec),
            static_cast<long>(frame->timestamp.tv_usec));
        unsigned long sendStarted = millis();
        esp_err_t result = httpd_resp_send_chunk(request, BOUNDARY, sizeof(BOUNDARY) - 1);
        if (result == ESP_OK)
            result = httpd_resp_send_chunk(request, header, length);
        if (result == ESP_OK)
            result = httpd_resp_send_chunk(request,
                reinterpret_cast<const char *>(frame->buf), frame->len);
        esp_camera_fb_return(frame);
        sendMs.store(millis() - sendStarted, std::memory_order_relaxed);
        if (result != ESP_OK) return result;
        framesSent.fetch_add(1, std::memory_order_relaxed);
        unsigned long elapsed = millis() - captureStarted;
        delay(elapsed < FRAME_INTERVAL_MS ? FRAME_INTERVAL_MS - elapsed : 1);
    }
    return ESP_FAIL;
}

static esp_err_t status(httpd_req_t *request) {
    char body[400];
    snprintf(body, sizeof(body),
        "{\"camera\":\"OV2640\",\"width\":640,\"height\":480,"
        "\"psram_bytes\":%u,\"rssi_dbm\":%d,\"stream_port\":81,"
        "\"frames_sent\":%u,\"capture_ms\":%u,\"send_ms\":%u,"
        "\"jpeg_quality\":%d,\"max_fps\":%lu,\"snapshots_sent\":%u,\"uptime_ms\":%lu,"
        "\"udp_port\":82,\"udp_frames_sent\":%u}",
        static_cast<unsigned>(ESP.getPsramSize()), WiFi.RSSI(),
        static_cast<unsigned>(framesSent.load(std::memory_order_relaxed)),
        static_cast<unsigned>(captureMs.load(std::memory_order_relaxed)),
        static_cast<unsigned>(sendMs.load(std::memory_order_relaxed)), JPEG_QUALITY,
        1000UL / FRAME_INTERVAL_MS,
        static_cast<unsigned>(snapshotsSent.load(std::memory_order_relaxed)), millis(),
        static_cast<unsigned>(udpFramesSent.load(std::memory_order_relaxed)));
    httpd_resp_set_type(request, "application/json");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    return httpd_resp_send(request, body, HTTPD_RESP_USE_STRLEN);
}

// A bounded, complete JPEG response avoids a long-lived MJPEG/TCP backlog.
// The Mac requests the next picture only after receiving this one.
static esp_err_t snapshot(httpd_req_t *request) {
    int noDelay = 1;
    setsockopt(httpd_req_to_sockfd(request), IPPROTO_TCP, TCP_NODELAY,
               &noDelay, sizeof(noDelay));
    unsigned long started = millis();
    camera_fb_t *frame = esp_camera_fb_get();
    captureMs.store(millis() - started, std::memory_order_relaxed);
    if (!frame) {
        httpd_resp_set_status(request, "503 Service Unavailable");
        return httpd_resp_send(request, "Camera unavailable", HTTPD_RESP_USE_STRLEN);
    }
    if (frame->format != PIXFORMAT_JPEG || frame->len < 4) {
        esp_camera_fb_return(frame);
        return ESP_FAIL;
    }
    char timestamp[40];
    snprintf(timestamp, sizeof(timestamp), "%ld.%06ld",
             static_cast<long>(frame->timestamp.tv_sec),
             static_cast<long>(frame->timestamp.tv_usec));
    httpd_resp_set_type(request, "image/jpeg");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    httpd_resp_set_hdr(request, "X-Timestamp", timestamp);
    started = millis();
    esp_err_t result = httpd_resp_send(request,
        reinterpret_cast<const char *>(frame->buf), frame->len);
    sendMs.store(millis() - started, std::memory_order_relaxed);
    esp_camera_fb_return(frame);
    if (result == ESP_OK) snapshotsSent.fetch_add(1, std::memory_order_relaxed);
    return result;
}

static esp_err_t index(httpd_req_t *request) {
    httpd_resp_set_type(request, "text/plain");
    return httpd_resp_send(request,
        "Granny camera ready.\nUse http://THIS_CAMERA_IP/capture in Granny.\n"
        "Legacy MJPEG: http://THIS_CAMERA_IP:81/stream\n"
        "One video viewer at a time. Camera details: /status\n",
        HTTPD_RESP_USE_STRLEN);
}

static bool startServers() {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 80;
    config.max_open_sockets = 3;
    config.lru_purge_enable = true;
    config.send_wait_timeout = 3;
    config.recv_wait_timeout = 3;
    if (httpd_start(&statusServer, &config) != ESP_OK) return false;
    httpd_uri_t root = {};
    root.uri = "/";
    root.method = HTTP_GET;
    root.handler = index;
    httpd_uri_t health = {};
    health.uri = "/status";
    health.method = HTTP_GET;
    health.handler = status;
    httpd_uri_t photo = {};
    photo.uri = "/capture";
    photo.method = HTTP_GET;
    photo.handler = snapshot;
    if (httpd_register_uri_handler(statusServer, &root) != ESP_OK ||
        httpd_register_uri_handler(statusServer, &health) != ESP_OK ||
        httpd_register_uri_handler(statusServer, &photo) != ESP_OK) return false;

    config.server_port = 81;
    config.ctrl_port += 1;
    if (httpd_start(&streamServer, &config) != ESP_OK) return false;
    httpd_uri_t video = {};
    video.uri = "/stream";
    video.method = HTTP_GET;
    video.handler = stream;
    return httpd_register_uri_handler(streamServer, &video) == ESP_OK;
}

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("\nGranny GOOUUU S3 camera starting.");
    if (!WIFI_SSID[0]) halt("Wi-Fi not configured. Run python3 esp32/setup_wifi.py, then rebuild/upload.");
    if (!psramFound()) halt("PSRAM initialization failed. Check the qio_opi / BOARD_HAS_PSRAM build settings.");
    Serial.printf("PSRAM: %u bytes\n", static_cast<unsigned>(ESP.getPsramSize()));

    camera_config_t camera = {};
    camera.ledc_channel = LEDC_CHANNEL_0;
    camera.ledc_timer = LEDC_TIMER_0;
    camera.pin_d0 = 11;
    camera.pin_d1 = 9;
    camera.pin_d2 = 8;
    camera.pin_d3 = 10;
    camera.pin_d4 = 12;
    camera.pin_d5 = 18;
    camera.pin_d6 = 17;
    camera.pin_d7 = 16;
    camera.pin_xclk = 15;
    camera.pin_pclk = 13;
    camera.pin_vsync = 6;
    camera.pin_href = 7;
    camera.pin_sccb_sda = 4;
    camera.pin_sccb_scl = 5;
    camera.pin_pwdn = -1;
    camera.pin_reset = -1;
    camera.xclk_freq_hz = 20000000;
    camera.pixel_format = PIXFORMAT_JPEG;
    camera.frame_size = FRAMESIZE_VGA;
    camera.jpeg_quality = JPEG_QUALITY;
    camera.fb_count = 2;
    camera.fb_location = CAMERA_FB_IN_PSRAM;
    camera.grab_mode = CAMERA_GRAB_LATEST;
    esp_err_t result = esp_camera_init(&camera);
    if (result != ESP_OK) {
        Serial.printf("Camera initialization failed: 0x%x\n", result);
        halt("Power off before checking the camera connector.");
    }
    sensor_t *sensor = esp_camera_sensor_get();
    if (!sensor || sensor->id.PID != OV2640_PID)
        halt("Unexpected camera sensor. This firmware targets OV2640.");
    Serial.println("OV2640 initialized at 640 x 480.");

    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.setSleep(false);
    WiFi.onEvent([](arduino_event_id_t, arduino_event_info_t info) {
        // Report only the reason code, never credentials or device addresses.
        Serial.printf("Wi-Fi disconnected: reason=%u\n", info.wifi_sta_disconnected.reason);
    }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
    // Limit the scan to the configured network; no neighboring names are logged.
    int found = WiFi.scanNetworks(false, false, false, 300, 0, WIFI_SSID);
    Serial.printf("Saved hotspot visible on 2.4 GHz: %s\n", found > 0 ? "yes" : found == 0 ? "no" : "scan failed");
    if (found > 0)
        Serial.printf("Hotspot channel=%d, signal=%d dBm\n", WiFi.channel(0), WiFi.RSSI(0));
    WiFi.scanDelete();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.println("Connecting to configured Wi-Fi...");
    unsigned long started = millis();
    while (WiFi.status() != WL_CONNECTED) {
        delay(250);
        if (millis() - started >= 30000) {
            Serial.printf("Wi-Fi still unavailable (status=%d); retrying. Check hotspot settings.\n", WiFi.status());
            WiFi.reconnect();
            started = millis();
        }
    }
    if (!startServers()) halt("Camera HTTP server failed to start.");
    if (!videoUdp.begin(82)) halt("Camera UDP server failed to start.");
    Serial.printf("GRANNY_STREAM_URL=http://%s:81/stream\n", WiFi.localIP().toString().c_str());
    Serial.printf("GRANNY_SNAPSHOT_URL=http://%s/capture\n", WiFi.localIP().toString().c_str());
}

void loop() {
    // One requested image at a time, split into small datagrams. Lost pieces
    // are discarded by the Mac rather than holding newer images behind TCP
    // retransmissions. The request nonce prevents mixing different pictures.
    int length = videoUdp.parsePacket();
    if (length <= 0) { delay(1); return; }
    uint32_t request[2];
    IPAddress destination = videoUdp.remoteIP();
    uint16_t port = videoUdp.remotePort();
    if (length != sizeof(request) || videoUdp.read(reinterpret_cast<uint8_t *>(request), sizeof(request)) != sizeof(request)
            || ntohl(request[0]) != 0x47524551) {
        videoUdp.flush();
        return;
    }
    camera_fb_t *frame = esp_camera_fb_get();
    if (!frame) return;
    if (frame->format == PIXFORMAT_JPEG && frame->len >= 4 && frame->len <= 2 * 1024 * 1024) {
        bool okay = true;
        for (size_t offset = 0; offset < frame->len; offset += 1200) {
            uint32_t header[6] = {htonl(0x474a5047), request[1], htonl(frame->len), htonl(offset),
                htonl(frame->timestamp.tv_sec), htonl(frame->timestamp.tv_usec)};
            size_t count = min(static_cast<size_t>(1200), frame->len - offset);
            if (!videoUdp.beginPacket(destination, port)
                    || videoUdp.write(reinterpret_cast<uint8_t *>(header), sizeof(header)) != sizeof(header)
                    || videoUdp.write(frame->buf + offset, count) != count
                    || !videoUdp.endPacket()) { okay = false; break; }
            delay(1);
        }
        if (okay) udpFramesSent.fetch_add(1, std::memory_order_relaxed);
    }
    esp_camera_fb_return(frame);
}

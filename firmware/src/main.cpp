#include <Arduino.h>
#include <Wire.h>
#include <lvgl.h>
#include <ArduinoJson.h>
#include <esp_heap_caps.h>

#include "data.h"
#include "ui.h"
#include "usb_comm.h"
#include "splash.h"
#include "usage_rate.h"
#include "idle.h"
#include "idle_cfg.h"

#include "hal/board_caps.h"
#include "hal/display_hal.h"
#include "hal/touch_hal.h"
#include "hal/input_hal.h"
#include "hal/power_hal.h"
#include "hal/imu_hal.h"

static UsageData usage = {};

// HID keyboard output can be disabled at runtime via {"hid":false} over serial,
// or at compile time with -DHID_BUTTONS_DEFAULT=0. PWR button (screen cycling)
// is always active regardless of this flag.
#ifndef HID_BUTTONS_DEFAULT
#define HID_BUTTONS_DEFAULT 1
#endif
static bool hid_enabled = HID_BUTTONS_DEFAULT;

// ---- LVGL draw buffers (PSRAM, partial render mode) ----
#define BUF_LINES 40
static uint16_t* buf1 = nullptr;
static uint16_t* buf2 = nullptr;

static uint32_t my_tick(void) { return millis(); }

static void my_flush_cb(lv_display_t* disp, const lv_area_t* area, uint8_t* px_map) {
    int32_t w = area->x2 - area->x1 + 1;
    int32_t h = area->y2 - area->y1 + 1;
    display_hal_draw_bitmap(area->x1, area->y1, w, h, (uint16_t*)px_map);
    lv_display_flush_ready(disp);
}

static void rounder_cb(lv_event_t* e) {
    lv_area_t* area = (lv_area_t*)lv_event_get_param(e);
    display_hal_round_area(&area->x1, &area->y1, &area->x2, &area->y2);
}

// Touch policy is driven by IDLE_WAKE_ON_TOUCH:
//   true  → a press edge while asleep wakes the device and the first touch is
//           swallowed (mirrors the button wake-consumption); a press while
//           awake counts as activity.
//   false → touch never counts as activity and is fully swallowed while the
//           panel is dark, so pets/sleeves can't wake it overnight and LVGL
//           can't quietly toggle splash<->usage on a black panel.
static void my_touch_cb(lv_indev_t* indev, lv_indev_data_t* data) {
    uint16_t x, y;
    bool pressed;
    touch_hal_read(&x, &y, &pressed);
    const bool raw_pressed = pressed;

    if (IDLE_WAKE_ON_TOUCH) {
        static bool touch_was = false;
        static bool touch_wake_swallowed = false;
        if (raw_pressed && !touch_was) {
            // Press edge — consume as wake if asleep.
            if (idle_consume_wake_press()) {
                touch_wake_swallowed = true;
                pressed = false;
            }
        } else if (!raw_pressed && touch_was) {
            // Release edge.
            if (touch_wake_swallowed) {
                touch_wake_swallowed = false;
                pressed = false;
            }
        } else if (raw_pressed && touch_wake_swallowed) {
            // Held finger through wake — keep hiding until release.
            pressed = false;
        }
        touch_was = raw_pressed;
    } else if (idle_is_asleep()) {
        pressed = false;
    }

    if (pressed) {
        data->point.x = x;
        data->point.y = y;
        data->state = LV_INDEV_STATE_PRESSED;
    } else {
        data->state = LV_INDEV_STATE_RELEASED;
    }
}

// Try to handle a JSON line as a config command.
// Returns true if the line was a config command (consumed), false otherwise.
static bool try_parse_config(const char* json) {
    JsonDocument doc;
    if (deserializeJson(doc, json)) return false;
    if (!doc.containsKey("hid")) return false;

    hid_enabled = doc["hid"] | false;
    Serial.printf("HID buttons %s\n", hid_enabled ? "enabled" : "disabled");
    return true;
}

// Parse a JSON line into UsageData.
static bool parse_json(const char* json, UsageData* out) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, json);
    if (err) {
        Serial.printf("JSON parse error: %s\n", err.c_str());
        return false;
    }

    out->session_pct = doc["s"] | 0.0f;
    out->session_reset_mins = doc["sr"] | -1;
    out->weekly_pct = doc["w"] | 0.0f;
    out->weekly_reset_mins = doc["wr"] | -1;
    strlcpy(out->status, doc["st"] | "unknown", sizeof(out->status));
    out->ok = doc["ok"] | false;
    out->valid = true;
    return true;
}

// Screenshot command — called from usb_comm_tick() when "screenshot" is received.
// Declared extern in usb_comm.cpp so the USB module can invoke it.
void send_screenshot() {
    const uint32_t w = board_caps().width;
    const uint32_t h = board_caps().height;
    const uint32_t row_bytes = w * 2;
    const uint32_t buf_size = row_bytes * h;
    uint8_t* sbuf = (uint8_t*)heap_caps_malloc(buf_size, MALLOC_CAP_SPIRAM);
    if (!sbuf) {
        Serial.println("SCREENSHOT_ERR");
        return;
    }

    lv_draw_buf_t draw_buf;
    lv_draw_buf_init(&draw_buf, w, h, LV_COLOR_FORMAT_RGB565, row_bytes, sbuf, buf_size);

    lv_result_t res = lv_snapshot_take_to_draw_buf(lv_screen_active(), LV_COLOR_FORMAT_RGB565, &draw_buf);
    if (res != LV_RESULT_OK) {
        heap_caps_free(sbuf);
        Serial.println("SCREENSHOT_ERR");
        return;
    }

    Serial.printf("SCREENSHOT_START %lu %lu %lu\n",
        (unsigned long)w, (unsigned long)h, (unsigned long)buf_size);
    Serial.flush();
    Serial.write(sbuf, buf_size);
    Serial.flush();
    Serial.println();
    Serial.println("SCREENSHOT_END");
    heap_caps_free(sbuf);
}

// Each board provides this. Must bring up the shared I2C bus (Wire.begin
// with the board's SDA/SCL pins) and any board-private hardware that has
// to settle before display/touch (e.g. an IO expander gating the LCD
// reset line). Called exactly once at the start of setup().
extern "C" void board_init(void);

void setup() {
    // USB composite init MUST happen before any Serial use,
    // because CDC_ON_BOOT=0 means Serial is UART0 by default.
    // With -DSerial=USBSerial, all Serial calls route through
    // our USBCDC instance after this point.
    usb_comm_init();
    delay(300);
    Serial.println("{\"ready\":true}");

    board_init();

    display_hal_init();
    display_hal_begin();
    idle_init();   // takes over brightness (DISPLAY_DEFAULT_BRIGHTNESS) and starts the idle timer

    power_hal_init();
    imu_hal_init();
    touch_hal_init();

    // ---- LVGL ----
    const int W = board_caps().width;
    const int H = board_caps().height;

    lv_init();
    lv_tick_set_cb(my_tick);

    buf1 = (uint16_t*)heap_caps_malloc(W * BUF_LINES * 2, MALLOC_CAP_SPIRAM);
    buf2 = (uint16_t*)heap_caps_malloc(W * BUF_LINES * 2, MALLOC_CAP_SPIRAM);

    lv_display_t* disp = lv_display_create(W, H);
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB565);
    lv_display_set_flush_cb(disp, my_flush_cb);
    lv_display_set_buffers(disp, buf1, buf2, W * BUF_LINES * 2,
                           LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_add_event_cb(disp, rounder_cb, LV_EVENT_INVALIDATE_AREA, NULL);

    lv_indev_t* indev = lv_indev_create();
    lv_indev_set_type(indev, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(indev, my_touch_cb);

    input_hal_init();

    ui_init();
    ui_update_conn_status(usb_get_state(), usb_get_device_name(), usb_get_port_info());
    ui_update_battery(power_hal_battery_pct(), power_hal_is_charging());
    ui_show_screen(SCREEN_SPLASH);

    Serial.printf("Dashboard ready (%s, %dx%d), waiting for data on USB...\n",
        board_caps().name, W, H);
}

// Auto screen-switching on USB connect/disconnect:
//   plugged back in (CDC CONNECTED)         -> show the meter immediately
//   unplugged (CDC DISCONNECTED) for >5 min  -> show the animation
// Both are edge-triggered, so the PWR button can still freely cycle screens
// between connect/disconnect events without the animation re-asserting itself.
#define DISCONNECT_TO_SPLASH_MS  (5UL * 60UL * 1000UL)
static conn_state_t last_conn_state = CONN_STATE_INIT;
static uint32_t     disconnected_since_ms = 0;
static bool         disconnect_splash_armed = false;

void loop() {
    idle_tick();
    lv_timer_handler();
    ui_tick_anim();
    usb_comm_tick();
    power_hal_tick();
    imu_hal_tick();
    splash_tick();
    // Rotation transition (blank + ramp) would fight the idle fade — skip
    // ticks while the panel is dark. A rotation that happens during sleep
    // is detected by the next tick after wake and ramped in then.
    if (!idle_is_asleep()) display_hal_tick();

    // ---- Physical buttons ----
    //   PRIMARY   → HID Space  (Claude Code voice-mode PTT)
    //   SECONDARY → HID Shift+Tab  (mode toggle; only if the board has one)
    //   PWR       → cycle screens; on splash, cycle animations
    // First press from sleep is consumed as a wake-only event by
    // idle_consume_wake_press(); the normal action fires from the second
    // press. Activity bookkeeping happens inside idle_consume_wake_press
    // so no separate idle_note_activity() call is needed here.
    {
        static bool primary_was = false;
        static bool primary_wake_swallowed = false;
        bool primary_now = input_hal_is_held(INPUT_BTN_PRIMARY);
        if (primary_now != primary_was) {
            if (primary_now) {
                if (idle_consume_wake_press()) primary_wake_swallowed = true;
                else if (hid_enabled)           usb_keyboard_press(0x2C, 0);  // HID Space, no mods
            } else {
                if (primary_wake_swallowed) primary_wake_swallowed = false;
                else if (hid_enabled)       usb_keyboard_release();
            }
            primary_was = primary_now;
        }

        if (board_caps().button_count >= 2) {
            static bool secondary_was = false;
            static bool secondary_wake_swallowed = false;
            bool secondary_now = input_hal_is_held(INPUT_BTN_SECONDARY);
            if (secondary_now != secondary_was) {
                if (secondary_now) {
                    if (idle_consume_wake_press()) secondary_wake_swallowed = true;
                    else if (hid_enabled)           usb_keyboard_press(0x2B, 0x02);  // HID Tab + LEFT_SHIFT
                } else {
                    if (secondary_wake_swallowed) secondary_wake_swallowed = false;
                    else if (hid_enabled)          usb_keyboard_release();
                }
                secondary_was = secondary_now;
            }
        }

        if (power_hal_pwr_pressed()) {
            if (!idle_consume_wake_press()) {
                if (ui_get_current_screen() == SCREEN_SPLASH) splash_next();
                else                                          ui_cycle_screen();
            }
        }
    }

    conn_state_t cs = usb_get_state();
    if (cs != last_conn_state) {
        last_conn_state = cs;
        ui_update_conn_status(cs, usb_get_device_name(), usb_get_port_info());

        if (cs == CONN_STATE_CONNECTED) {
            // Plugged back in — return to the meter right away.
            disconnect_splash_armed = false;
            ui_show_screen(SCREEN_USAGE);
        } else if (cs == CONN_STATE_DISCONNECTED) {
            // Start the unplugged timer; the switch to the animation fires
            // once it elapses (see below).
            disconnected_since_ms = millis();
            disconnect_splash_armed = true;
        }
    }

    // Unplugged for longer than the timeout — switch to the animation once.
    // millis() wraparound is safe under unsigned subtraction.
    if (disconnect_splash_armed &&
        (millis() - disconnected_since_ms) >= DISCONNECT_TO_SPLASH_MS) {
        disconnect_splash_armed = false;
        ui_show_screen(SCREEN_SPLASH);
    }

    static int  last_pct      = -2;
    static bool last_charging = false;
    int  pct      = power_hal_battery_pct();
    bool charging = power_hal_is_charging();
    if (pct != last_pct || charging != last_charging) {
        last_pct = pct;
        last_charging = charging;
        ui_update_battery(pct, charging);
    }

    // Serial line handling is done inside usb_comm_tick() — no separate
    // check_serial_cmd() needed; JSON payloads and screenshot commands
    // both come over the same USB CDC serial line.

    if (usb_has_data()) {
        const char* raw = usb_get_data();
        if (try_parse_config(raw)) {
            usb_send_ack();
        } else if (parse_json(raw, &usage)) {
            int g_before = usage_rate_group();
            usage_rate_sample(usage.session_pct);
            int g_after = usage_rate_group();
            if (g_after != g_before) {
                Serial.printf("usage rate: group %d -> %d (s=%.2f%%)\n",
                    g_before, g_after, usage.session_pct);
                if (splash_is_active()) splash_pick_for_current_rate();
            }
            ui_update(&usage);
            usb_send_ack();
        } else {
            usb_send_nack();
        }
    }

    delay(5);
}

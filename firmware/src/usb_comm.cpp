// USB CDC + HID composite device — replaces ble.cpp for USB-only operation.
//
// The ESP32-S3 Arduino core creates a global `Serial` backed by UART0 when
// ARDUINO_USB_CDC_ON_BOOT=0. We build with `-DSerial=USBSerial` so every
// `Serial.println()` in shared code routes through our USBCDC instance
// instead. Inside this file we #undef the macro to avoid self-referencing
// the variable we're defining.

#undef Serial  // undo -DSerial=USBSerial for this TU

#include "usb_comm.h"
#include <Arduino.h>
#include "USB.h"
#include "USBCDC.h"
#include "USBHIDKeyboard.h"

#define DEVICE_NAME "Claude Controller"
#define USB_BUF_SIZE 512

// ---- globals used by the rest of the firmware via -DSerial=USBSerial ----
USBCDC USBSerial(0);

static USBHIDKeyboard Keyboard;

static conn_state_t state = CONN_STATE_INIT;
static volatile bool suspended = false;
static char rx_buf[USB_BUF_SIZE];
static volatile bool data_ready = false;
static volatile bool has_received_data = false;

// Line buffer for incoming serial data
static char line_buf[USB_BUF_SIZE];
static int  line_pos = 0;

// Forward declaration for screenshot (defined in main.cpp, called via serial)
extern void send_screenshot();

// ---- CDC connection event callback ----
static void cdc_event_cb(void* arg, esp_event_base_t base, int32_t id, void* data) {
    (void)arg;
    (void)base;
    (void)data;
    if (id == ARDUINO_USB_CDC_CONNECTED_EVENT) {
        state = CONN_STATE_CONNECTED;
    } else if (id == ARDUINO_USB_CDC_DISCONNECTED_EVENT) {
        state = CONN_STATE_DISCONNECTED;
    }
}

// ---- USB bus suspend/resume event callback ----
// Bus-level (not CDC-level) events. These fire when the host stops driving the
// bus even though VBUS is still present — the powered-hub-unplug and PC-sleep
// cases that the CDC connect/disconnect events miss.
static void usb_event_cb(void* arg, esp_event_base_t base, int32_t id, void* data) {
    (void)arg;
    (void)base;
    (void)data;
    if (id == ARDUINO_USB_SUSPEND_EVENT) {
        suspended = true;
    } else if (id == ARDUINO_USB_RESUME_EVENT) {
        suspended = false;
    }
}

void usb_comm_init(void) {
    // Configure USB device descriptors
    USB.productName(DEVICE_NAME);
    USB.manufacturerName("Clawdmeter");
    USB.VID(0x303A);   // Espressif Systems VID
    USB.PID(0x1001);   // Custom PID for Claude Controller

    // Register HID keyboard interface
    Keyboard.begin();

    // Register CDC interface and set up connection tracking
    USBSerial.begin(115200);
    USBSerial.onEvent(cdc_event_cb);

    // Track bus suspend/resume so we can detect a vanished host even when VBUS
    // stays up (powered hub) and no CDC disconnect is delivered.
    USB.onEvent(ARDUINO_USB_SUSPEND_EVENT, usb_event_cb);
    USB.onEvent(ARDUINO_USB_RESUME_EVENT, usb_event_cb);

    // Start the composite USB device (CDC + HID)
    USB.begin();

    // Give the host a moment to enumerate
    delay(500);
    state = CONN_STATE_DISCONNECTED;

    USBSerial.printf("USB: init complete, device=%s\n", DEVICE_NAME);
}

void usb_comm_tick(void) {
    // Read incoming serial data line by line.
    // Lines starting with '{' are treated as JSON data payloads.
    // The line "screenshot" triggers a screenshot dump.
    while (USBSerial.available()) {
        char c = USBSerial.read();
        if (c == '\n' || c == '\r') {
            if (line_pos == 0) continue;  // skip empty lines
            line_buf[line_pos] = '\0';

            if (line_buf[0] == '{') {
                // JSON payload — copy to rx_buf for consumption
                size_t len = line_pos;
                if (len >= USB_BUF_SIZE) len = USB_BUF_SIZE - 1;
                memcpy(rx_buf, line_buf, len);
                rx_buf[len] = '\0';
                data_ready = true;
                has_received_data = true;
            } else if (strcmp(line_buf, "screenshot") == 0) {
                send_screenshot();
            }
            // else: unknown command, ignore

            line_pos = 0;
        } else if (line_pos < USB_BUF_SIZE - 1) {
            line_buf[line_pos++] = c;
        }
    }
}

conn_state_t usb_get_state(void) {
    return state;
}

bool usb_is_suspended(void) {
    return suspended;
}

const char* usb_get_device_name(void) {
    return DEVICE_NAME;
}

const char* usb_get_port_info(void) {
    return "USB Serial";
}

bool usb_has_data(void) {
    return data_ready;
}

const char* usb_get_data(void) {
    data_ready = false;
    return rx_buf;
}

void usb_send_ack(void) {
    USBSerial.println("{\"ack\":true}");
}

void usb_send_nack(void) {
    USBSerial.println("{\"err\":true}");
}

void usb_request_refresh(void) {
    if (!has_received_data) {
        USBSerial.println("{\"refresh\":true}");
    }
}

// ---- USB HID keyboard ----
//
// The original BLE code sends raw HID usage codes:
//   0x2C = Space,  0x2B = Tab
// The Arduino USBHIDKeyboard class expects ASCII chars or KEY_* constants.
// We translate between the two conventions here.

void usb_keyboard_press(uint8_t hid_key, uint8_t modifier) {
    // Apply modifier keys
    if (modifier & 0x02) Keyboard.press(KEY_LEFT_SHIFT);
    if (modifier & 0x01) Keyboard.press(KEY_LEFT_CTRL);
    if (modifier & 0x04) Keyboard.press(KEY_LEFT_ALT);

    // Translate HID usage codes to Arduino key constants
    switch (hid_key) {
        case 0x2C: Keyboard.press(' ');        break;  // HID Space
        case 0x2B: Keyboard.press(KEY_TAB);    break;  // HID Tab
        case 0x28: Keyboard.press(KEY_RETURN); break;  // HID Enter
        case 0x29: Keyboard.press(KEY_ESC);    break;  // HID Escape
        default:
            // For standard letter keys (0x04-0x1D = a-z), offset to ASCII
            if (hid_key >= 0x04 && hid_key <= 0x1D) {
                Keyboard.press('a' + (hid_key - 0x04));
            }
            break;
    }
}

void usb_keyboard_release(void) {
    Keyboard.releaseAll();
}

#pragma once
#include <stdint.h>

enum conn_state_t {
    CONN_STATE_INIT,
    CONN_STATE_DISCONNECTED,
    CONN_STATE_CONNECTED,
};

void usb_comm_init(void);       // Call FIRST in setup(), before any Serial use
void usb_comm_tick(void);       // Call from loop()

conn_state_t usb_get_state(void);
const char*  usb_get_device_name(void);
const char*  usb_get_port_info(void);

bool        usb_has_data(void);
const char* usb_get_data(void);
void        usb_send_ack(void);
void        usb_send_nack(void);
void        usb_request_refresh(void);

// USB HID keyboard
void usb_keyboard_press(uint8_t key, uint8_t modifier);
void usb_keyboard_release(void);

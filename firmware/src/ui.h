#pragma once
#include "data.h"
#include "usb_comm.h"

enum screen_t {
    SCREEN_SPLASH,
    SCREEN_USAGE,
    SCREEN_CONNECTION,
    SCREEN_COUNT,
};

void ui_init(void);
void ui_update(const UsageData* data);
void ui_tick_anim(void);
void ui_show_screen(screen_t screen);
void ui_cycle_screen(void);
void ui_toggle_splash(void);
screen_t ui_get_current_screen(void);
void ui_update_conn_status(conn_state_t state, const char* name, const char* info);
void ui_update_battery(int percent, bool charging);

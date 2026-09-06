"""Refined desktop performance surface for Starrypad.

The desktop engine is deliberately left in ``drum_pad_native.py``. This file
subclasses it and gives the performance screen the same hierarchy as the iOS
instrument: pads do not move when a panel opens, Mixer/Sampler are modes rather
than destinations, banks are direct buttons, and Rec/Play are the controls that
win the visual hierarchy.

Step Seq and the deeper desktop editors remain the existing desktop screens.
"""

import sys
import time

import pygame

import drum_pad_native as core
import icons
import theme


HEADER_HEIGHT = 62
MODE_TOP = 72
MODE_HEIGHT = 40
GRID_TOP = 128
GRID_LEFT = 16
PAD_SIZE = 126
PAD_GAP = 8
SIDE_LEFT = 560
SIDE_WIDTH = 464
SIDE_BOTTOM = 656
PAD_RADIUS = 10


class StarrypadDesktop(core.DrumPadNative):
    """Desktop engine with the quieter, performance-first Starrypad surface."""

    def __init__(self):
        super().__init__()
        self.desktop_panel = None
        self.desktop_pads_locked = False

    def pad_rects(self):
        rects = {}
        for index in range(len(core.PADS)):
            row = 3 - (index // 4)
            col = index % 4
            rects[index] = pygame.Rect(
                GRID_LEFT + col * (PAD_SIZE + PAD_GAP),
                GRID_TOP + row * (PAD_SIZE + PAD_GAP),
                PAD_SIZE,
                PAD_SIZE,
            )
        return rects

    def begin_pad_drag(self, index, pos):
        if self.desktop_pads_locked:
            return
        return super().begin_pad_drag(index, pos)

    def _desktop_overlay_open(self):
        return any((
            self.clip_prompt_open, self.browser_open, self.chop_open,
            self.sample_editor_open, self.share_open, self.perform_fx_open,
            self.mixer_open, self.scene_open, self.feel_open,
            self.project_menu_open, self.settings_open,
        ))

    def handle_mouse(self, pos, modifiers=0):
        if not self._desktop_overlay_open() and self.main_tab != "Step Seq":
            hit = next(
                (name for name, rect in self.buttons.items() if rect.collidepoint(pos)),
                None,
            )
            if hit == "desktop_mixer":
                self.desktop_panel = None if self.desktop_panel == "mixer" else "mixer"
                return
            if hit == "desktop_sampler":
                self.desktop_panel = None if self.desktop_panel == "sampler" else "sampler"
                return
            if hit == "desktop_lock":
                self.desktop_pads_locked = not self.desktop_pads_locked
                self.set_surface_notice(
                    "Pads locked - drag edits off" if self.desktop_pads_locked
                    else "Pads unlocked - drag to swap"
                )
                return
            if hit == "desktop_share":
                loop = self.loop_snapshot()
                if not loop["events"]:
                    self.set_surface_notice("Nothing recorded yet")
                elif loop["exporting"]:
                    self.set_surface_notice("Export already running")
                else:
                    self.share_open = True
                return
            if hit == "desktop_mix_volume_down":
                self.adjust_pad_mix("volume", -0.05)
                return
            if hit == "desktop_mix_volume_up":
                self.adjust_pad_mix("volume", 0.05)
                return
            if hit == "desktop_mix_pan_down":
                self.adjust_pad_mix("pan", -0.1)
                return
            if hit == "desktop_mix_pan_up":
                self.adjust_pad_mix("pan", 0.1)
                return
            if hit == "desktop_mix_tune_down":
                self.adjust_pad_mix("tune", -1)
                return
            if hit == "desktop_mix_tune_up":
                self.adjust_pad_mix("tune", 1)
                return
            if hit == "desktop_mix_mute":
                self.toggle_pad_mute()
                return
            if hit == "desktop_mix_solo":
                self.toggle_pad_solo()
                return
            if hit == "desktop_mix_reset":
                self.reset_pad_mix()
                return
            if hit == "desktop_mix_advanced":
                self.mixer_open = True
                return
            if hit == "desktop_sampler_record":
                self.toggle_sampling()
                return
            if hit == "desktop_sampler_browse":
                self.browser_open = True
                self.browser_selected = None
                self.browser_page = 0
                return
            if hit == "desktop_sampler_preview":
                self.queue_pad(self.selected_pad, 100)
                return
            if hit == "desktop_sampler_edit":
                if self.custom_sample_files[self.selected_pad]:
                    self.sample_editor_open = True
                else:
                    self.set_surface_notice("No sample on this pad")
                return
            if hit == "desktop_sampler_clear":
                if self.custom_sample_files[self.selected_pad]:
                    self.clear_custom_sample()
                return
        return super().handle_mouse(pos, modifiers)

    def draw(self):
        if self.main_tab == "Step Seq":
            return super().draw()

        self.buttons = {}
        self.settings_buttons = {}
        self.project_buttons = {}
        self.feel_buttons = {}
        self.scene_buttons = {}
        self.mixer_buttons = {}
        self.share_buttons = {}
        self.perform_fx_buttons = {}
        self.sample_editor_buttons = {}
        self.chop_buttons = {}
        self.browser_buttons = {}
        self.clip_prompt_buttons = {}
        self.screen.fill(theme.GROUND)
        if self.grain:
            self.screen.blit(self.grain, (0, 0))

        self.draw_desktop_header()
        self.draw_desktop_mode_row()
        self.draw_desktop_pads()
        if self.desktop_panel == "mixer":
            self.draw_desktop_mixer()
        elif self.desktop_panel == "sampler":
            self.draw_desktop_sampler()
        else:
            self.draw_desktop_loop()
            self.draw_desktop_deck()
            self.draw_desktop_pad_panel()

        self.draw_desktop_notice()
        self._draw_existing_overlays()
        self.draw_tooltip()
        self.present_screen()

    def _draw_existing_overlays(self):
        if self.browser_open:
            self.draw_sample_browser()
        if self.settings_open:
            self.draw_settings_overlay()
        if self.project_menu_open:
            self.draw_project_overlay()
        if self.feel_open:
            self.draw_feel_overlay()
        if self.scene_open:
            self.draw_scene_overlay()
        if self.mixer_open:
            self.draw_mixer_overlay()
        if self.perform_fx_open:
            self.draw_perform_fx_overlay()
        if self.share_open:
            self.draw_share_overlay()
        if self.sample_editor_open:
            self.draw_sample_editor_overlay()
        if self.chop_open:
            self.draw_chop_overlay()
        if self.clip_prompt_open:
            self.draw_clip_prompt()

    def draw_desktop_header(self):
        bar = pygame.Rect(0, 0, core.WINDOW_SIZE[0], HEADER_HEIGHT)
        pygame.draw.rect(self.screen, theme.PANEL, bar)
        pygame.draw.line(
            self.screen, theme.RULE,
            (0, HEADER_HEIGHT), (core.WINDOW_SIZE[0], HEADER_HEIGHT),
        )
        brand = self.head_font.render("STARRYPAD", True, theme.ACCENT)
        self.screen.blit(brand, (18, 20))

        self.buttons["project"] = pygame.Rect(142, 13, 228, 36)
        self.draw_button(
            self.buttons["project"], self.project_name[:22] or "Untitled",
            icon="folder",
        )

        name, dot, hint = (
            self.midi_activity() if self.midi_input
            else ("No MIDI", theme.DANGER, "")
        )
        midi = pygame.Rect(386, 13, 300, 36)
        pygame.draw.rect(
            self.screen, theme.PANEL_2, midi,
            border_radius=theme.RADIUS["field"],
        )
        pygame.draw.rect(
            self.screen, theme.RULE, midi, width=1,
            border_radius=theme.RADIUS["field"],
        )
        pygame.draw.circle(self.screen, dot, (midi.x + 15, midi.centery), 4)
        label = self.fit_text(self.small_font, f"MIDI  {name}", 210, theme.INK_2)
        self.screen.blit(label, (midi.x + 28, midi.centery - label.get_height() // 2))
        if self.last_midi_event_ns:
            note = self.data_font_sm.render(self.last_velocity[:16], True, theme.INK_3)
            self.screen.blit(
                note,
                (midi.right - note.get_width() - 10,
                 midi.centery - note.get_height() // 2),
            )
        if hint:
            self.set_surface_notice(hint, duration=1.0)

        loop = self.loop_snapshot()
        self.buttons["desktop_share"] = pygame.Rect(848, 13, 48, 36)
        self.buttons["desktop_lock"] = pygame.Rect(904, 13, 64, 36)
        self.buttons["settings"] = pygame.Rect(976, 13, 48, 36)
        self.draw_button(
            self.buttons["desktop_share"], "", icon="share",
            enabled=not loop["exporting"],
        )
        self.draw_button(
            self.buttons["desktop_lock"], "Lock",
            active=self.desktop_pads_locked,
        )
        self.draw_button(self.buttons["settings"], "", icon="gear")

    def draw_desktop_mode_row(self):
        y = MODE_TOP
        self.buttons["desktop_mixer"] = pygame.Rect(16, y, 132, MODE_HEIGHT)
        self.buttons["desktop_sampler"] = pygame.Rect(156, y, 132, MODE_HEIGHT)
        self.buttons["tab_Step Seq"] = pygame.Rect(296, y, 108, MODE_HEIGHT)
        self.draw_button(
            self.buttons["desktop_mixer"], "Mixer",
            active=self.desktop_panel == "mixer", icon="sliders",
        )
        self.draw_button(
            self.buttons["desktop_sampler"], "Sampler",
            active=self.desktop_panel == "sampler", icon="microphone",
        )
        self.draw_button(self.buttons["tab_Step Seq"], "Sequence", icon="grid")

        self.screen.blit(self.label_font.render("BANK", True, theme.INK_3), (430, y + 13))
        x = 476
        for column, bank in enumerate(core.PAD_BANKS):
            rect = pygame.Rect(x + column * 46, y, 40, MODE_HEIGHT)
            self.buttons[f"bank_{column}"] = rect
            self.draw_button(rect, bank, active=column == self.pad_bank)

        self.screen.blit(self.label_font.render("KIT", True, theme.INK_3), (686, y + 13))
        x = 720
        for column, slot in enumerate(core.KIT_SLOTS):
            rect = pygame.Rect(x + column * 46, y, 40, MODE_HEIGHT)
            self.buttons[f"kit_{slot}"] = rect
            self.draw_button(rect, slot, active=slot == self.active_kit)
        kit_name = core.KIT_NAMES.get(self.active_kit, self.active_kit)
        kit = self.fit_text(self.small_font, kit_name, 112, theme.INK_3)
        self.screen.blit(kit, (908, y + 11))

    def draw_desktop_pads(self):
        now = time.perf_counter()
        rects = self.pad_rects()
        for pad, rect in rects.items():
            index = self.slot(pad)
            if now < self.hit_until[index] and not self.pad_mute[index]:
                self.draw_pad_glow(rect.move(0, 2), self.hit_energy[index])

        for pad, base_rect in rects.items():
            index = self.slot(pad)
            muted = self.pad_mute[index]
            soloed = index in self.solo_pads
            selected = index in self.pad_selection
            sounding = now < self.hit_until[index]
            dragging_from = index == self.pad_drag_from and self.pad_drag_active
            drop_target = index == self.pad_drag_over
            flash = max(
                0.0,
                (self.pad_swap_flash.get(index, 0.0) - now)
                / core.PAD_SWAP_FLASH_SECONDS,
            )
            energy = self.hit_energy[index] if sounding else 0.0
            rect = base_rect.move(0, 2) if sounding else base_rect
            face = theme.PAD_HIT if sounding or drop_target else theme.PAD
            if sounding:
                face = theme.mix(face, theme.ACCENT_SOFT, 0.35 + 0.55 * energy)
            border = theme.ACCENT if sounding or selected or drop_target else theme.RULE
            if flash:
                face = theme.mix(face, theme.ACCENT_SOFT, flash)
                border = theme.mix(border, theme.ACCENT, flash)
            if muted:
                face, border = theme.dim(face, 0.4), theme.dim(border, 0.6)
            if dragging_from:
                face, border = theme.mix(theme.PAD, theme.GROUND, 0.5), theme.INK_3

            pygame.draw.rect(self.screen, face, rect, border_radius=PAD_RADIUS)
            custom_file = self.custom_sample_files[index]
            has_sample = bool(custom_file and custom_file in self.custom_sound_cache)
            hue = (
                theme.SIGNAL if has_sample
                else core.synth_color(
                    self.pad_synths[index], core.pad_profile(index)["color"]
                )
            )
            hue = theme.hue_hint(hue)
            if muted or dragging_from:
                hue = theme.dim(hue, 0.6)
            pygame.draw.rect(
                self.screen, hue,
                pygame.Rect(rect.x + 2, rect.y + 2, rect.width - 4, 3),
                border_radius=2,
            )

            edge = 3 if selected else 2 if sounding or drop_target or flash else 1
            pygame.draw.rect(
                self.screen, border, rect, width=edge,
                border_radius=PAD_RADIUS,
            )
            if drop_target:
                icons.draw(
                    self.screen, "swap",
                    pygame.Rect(rect.centerx - 12, rect.top + 18, 24, 24),
                    theme.ACCENT,
                )

            name_color = (
                theme.ACCENT if sounding or drop_target
                else theme.INK if selected else theme.INK_2
            )
            if muted or dragging_from:
                name_color = theme.INK_3
            label = (
                "SAMPLE" if has_sample
                else core.SYNTH_LABELS[self.pad_synths[index]].upper()
            )
            surface = self.fit_text(self.pad_font, label, rect.width - 18, name_color)
            self.screen.blit(surface, surface.get_rect(center=rect.center))

            note = core.PAD_TO_GM_NOTE.get(pad)
            if note is not None:
                number = self.data_font_sm.render(str(note), True, theme.INK_3)
                self.screen.blit(
                    number,
                    (rect.right - number.get_width() - 10,
                     rect.bottom - number.get_height() - 10),
                )
            if muted or soloed:
                marker = self.data_font_sm.render(
                    "M" if muted else "S", True, theme.INK_2
                )
                self.screen.blit(marker, (rect.x + 10, rect.y + 10))
        self.draw_pad_ghost()

    def _panel_shell(self, rect, title, subtitle=None):
        pygame.draw.rect(
            self.screen, theme.PANEL, rect,
            border_radius=theme.RADIUS["panel"],
        )
        pygame.draw.rect(
            self.screen, theme.RULE, rect, width=1,
            border_radius=theme.RADIUS["panel"],
        )
        self.screen.blit(
            self.label_font.render(title.upper(), True, theme.INK_3),
            (rect.x + 16, rect.y + 14),
        )
        if subtitle:
            text = self.fit_text(self.font, subtitle, rect.width - 32, theme.INK)
            self.screen.blit(text, (rect.x + 16, rect.y + 34))

    def draw_desktop_loop(self):
        rect = pygame.Rect(SIDE_LEFT, GRID_TOP, SIDE_WIDTH, 150)
        self._panel_shell(rect, "Loop")
        loop = self.loop_snapshot()
        state = (
            "REC" if loop["recording"] or loop["record_pending"]
            else "PLAY" if loop["playing"] else "STOP"
        )
        tone = (
            theme.DANGER if state == "REC"
            else theme.ACCENT if state == "PLAY" else theme.INK_3
        )
        badge = self.data_font_sm.render(state, True, tone)
        self.screen.blit(badge, (rect.right - badge.get_width() - 16, rect.y + 13))

        track = pygame.Rect(rect.x + 16, rect.y + 42, rect.width - 32, 30)
        pygame.draw.rect(self.screen, theme.GROUND, track, border_radius=4)
        pygame.draw.rect(self.screen, theme.RULE, track, width=1, border_radius=4)
        total_beats = max(1.0, loop["bars"] * 4.0)
        for beat in range(1, int(total_beats)):
            x = track.x + round(track.width * beat / total_beats)
            downbeat = beat % 4 == 0
            pygame.draw.line(
                self.screen,
                theme.RULE if downbeat else theme.RULE_SOFT,
                (x, track.y + (3 if downbeat else 9)),
                (x, track.bottom - (3 if downbeat else 9)),
            )
        for beat, pad_index, velocity in loop["events"]:
            x = track.x + round(track.width * beat / total_beats)
            height = 5 + round((velocity / 127.0) * 17)
            color = theme.hue_hint(core.synth_color(
                self.pad_synths[pad_index],
                core.PADS[self.pad_of(pad_index)]["color"],
            ))
            pygame.draw.rect(
                self.screen, color,
                pygame.Rect(x - 1, track.centery - height // 2, 2, height),
            )
        if loop["playing"] or loop["recording"] or loop["record_pending"]:
            x = track.x + round(track.width * loop["phase"] / total_beats)
            pygame.draw.line(
                self.screen, theme.ACCENT,
                (x, track.y + 2), (x, track.bottom - 2), 2,
            )

        y = rect.y + 86
        self.buttons["loop_bars"] = pygame.Rect(rect.x + 16, y, 50, 34)
        self.buttons["loop_repeat"] = pygame.Rect(rect.x + 72, y, 62, 34)
        self.draw_button(self.buttons["loop_bars"], f"{loop['bars']}B")
        self.draw_button(
            self.buttons["loop_repeat"],
            "Loop" if self.loop_repeat else "Once",
            active=self.loop_repeat,
        )

        page = self.pattern_page
        slots = range(
            page * core.PATTERN_PAGE,
            min(core.PATTERN_COUNT, (page + 1) * core.PATTERN_PAGE),
        )
        x0 = rect.x + 154
        for column, index in enumerate(slots):
            slot = pygame.Rect(x0 + column * 44, y, 38, 34)
            self.buttons[f"pattern_{index}"] = slot
            self.draw_button(
                slot, str(index + 1),
                active=index == self.active_pattern,
                danger=index == self.pending_pattern,
            )
            if (
                self.patterns[index] is not None
                and index not in (self.active_pattern, self.pending_pattern)
            ):
                pygame.draw.circle(
                    self.screen, theme.SIGNAL,
                    (slot.right - 6, slot.top + 6), 2,
                )
        pages = (core.PATTERN_COUNT + core.PATTERN_PAGE - 1) // core.PATTERN_PAGE
        self.buttons["pattern_page"] = pygame.Rect(rect.right - 58, y, 42, 34)
        self.draw_button(self.buttons["pattern_page"], f"{page + 1}/{pages}")

    def _draw_value_cell(self, rect, label, value, down_name, up_name, accent=False):
        pygame.draw.rect(self.screen, theme.PANEL_2, rect, border_radius=6)
        pygame.draw.rect(self.screen, theme.RULE, rect, width=1, border_radius=6)
        caption = self.label_font.render(label.upper(), True, theme.INK_3)
        self.screen.blit(caption, caption.get_rect(midtop=(rect.centerx, rect.y + 8)))
        reading = self.data_font_lg.render(
            str(value), True, theme.ACCENT if accent else theme.INK
        )
        self.screen.blit(reading, reading.get_rect(center=(rect.centerx, rect.y + 43)))
        self.buttons[down_name] = pygame.Rect(rect.x + 6, rect.bottom - 25, 30, 20)
        self.buttons[up_name] = pygame.Rect(rect.right - 36, rect.bottom - 25, 30, 20)
        self.draw_button(self.buttons[down_name], "-")
        self.draw_button(self.buttons[up_name], "+")

    def draw_desktop_deck(self):
        rect = pygame.Rect(SIDE_LEFT, 288, SIDE_WIDTH, 190)
        self._panel_shell(rect, "Deck")
        self._draw_value_cell(
            pygame.Rect(rect.x + 16, rect.y + 38, 92, 92),
            "Tempo", self.bpm, "bpm_down", "bpm_up", accent=True,
        )
        self._draw_value_cell(
            pygame.Rect(rect.x + 116, rect.y + 38, 92, 92),
            "Master", f"{round(self.volume * 100)}", "vol_down", "vol_up",
        )

        loop = self.loop_snapshot()
        recording = loop["record_pending"] or (
            loop["recording"] and not loop["overdub"]
        )
        self.buttons["loop_record"] = pygame.Rect(rect.x + 224, rect.y + 38, 104, 66)
        self.buttons["loop_play"] = pygame.Rect(rect.x + 336, rect.y + 38, 112, 66)
        self.draw_button(
            self.buttons["loop_record"], "Rec",
            danger=recording, icon="record",
        )
        self.draw_button(
            self.buttons["loop_play"], "Play",
            active=loop["playing"], icon="play",
        )

        items = (
            ("loop_undo", "Undo", "undo", loop["can_undo"]),
            ("loop_clear", "Clear", None, bool(loop["events"])),
            ("loop_overdub", "Overdub", "overdub", True),
            ("loop_stop", "Stop", "stop", True),
            ("loop_capture", "Capture", None, loop["can_capture"]),
            ("loop_quantize_feel", "Feel", "sliders", bool(loop["events"])),
        )
        y = rect.bottom - 48
        for column, (name, label, icon, enabled) in enumerate(items):
            button = pygame.Rect(rect.x + 16 + column * 72, y, 67, 34)
            self.buttons[name] = button
            lit = name == "loop_overdub" and loop["overdub"]
            self.draw_button(
                button, label,
                active=lit, danger=lit, enabled=enabled, icon=icon,
            )

    def draw_desktop_pad_panel(self):
        rect = pygame.Rect(SIDE_LEFT, 488, SIDE_WIDTH, 168)
        index = self.selected_pad
        name = (
            "Sample" if self.custom_sample_files[index]
            else core.SYNTH_LABELS[self.pad_synths[index]]
        )
        subtitle = (
            f"{core.PAD_BANKS[self.bank_of(index)]}"
            f"{self.pad_of(index) + 1}  {name}"
        )
        self._panel_shell(rect, "Selected pad", subtitle)
        self.draw_pad_mix_strip(rect.x + 16, rect.right - 16, rect.y + 64)

        y = rect.y + 108
        controls = (
            ("pad_mute", "Mute", self.pad_mute[index]),
            ("pad_solo", "Solo", index in self.solo_pads),
            ("browser", "Browse", False),
            ("sample", "Sample", False),
            ("repeat", "Repeat", self.repeat_enabled),
            ("metro", "Metro", self.metronome_enabled),
        )
        for column, (key, label, active) in enumerate(controls):
            button = pygame.Rect(rect.x + 16 + column * 72, y, 67, 36)
            self.buttons[key] = button
            icon = (
                "folder" if key == "browser"
                else "microphone" if key == "sample" else None
            )
            self.draw_button(
                button, label,
                active=active and key not in ("pad_mute", "metro"),
                danger=active and key in ("pad_mute", "metro"),
                enabled=key != "sample" or self.audio_inputs_available,
                icon=icon,
            )

    def draw_desktop_mixer(self):
        rect = pygame.Rect(
            SIDE_LEFT, GRID_TOP, SIDE_WIDTH, SIDE_BOTTOM - GRID_TOP
        )
        index = self.selected_pad
        name = (
            "Sample" if self.custom_sample_files[index]
            else core.SYNTH_LABELS[self.pad_synths[index]]
        )
        self._panel_shell(rect, "Mixer", name)
        self.screen.blit(
            self.small_font.render("Selected pad", True, theme.INK_3),
            (rect.x + 16, rect.y + 64),
        )

        pan = self.pad_pan[index]
        pan_value = (
            "C" if abs(pan) < 0.02
            else f"{'L' if pan < 0 else 'R'}{abs(round(pan * 100))}"
        )
        rows = (
            ("volume", "Level", f"{round(self.pad_volume[index] * 100)}%", rect.y + 104),
            ("pan", "Pan", pan_value, rect.y + 170),
            ("tune", "Tune", f"{self.pad_tune[index]:+d} st", rect.y + 236),
        )
        for field, label, value, y in rows:
            self.screen.blit(
                self.small_font.render(label, True, theme.INK_2),
                (rect.x + 16, y + 10),
            )
            self.buttons[f"desktop_mix_{field}_down"] = pygame.Rect(
                rect.x + 230, y, 42, 40
            )
            self.buttons[f"desktop_mix_{field}_up"] = pygame.Rect(
                rect.right - 58, y, 42, 40
            )
            self.draw_button(self.buttons[f"desktop_mix_{field}_down"], "-")
            self.draw_button(self.buttons[f"desktop_mix_{field}_up"], "+")
            reading = self.data_font.render(value, True, theme.INK)
            self.screen.blit(
                reading,
                reading.get_rect(center=(rect.x + 344, y + 20)),
            )

        y = rect.y + 318
        self.buttons["desktop_mix_mute"] = pygame.Rect(rect.x + 16, y, 98, 42)
        self.buttons["desktop_mix_solo"] = pygame.Rect(rect.x + 122, y, 98, 42)
        self.buttons["desktop_mix_reset"] = pygame.Rect(rect.x + 228, y, 98, 42)
        self.draw_button(
            self.buttons["desktop_mix_mute"], "Mute",
            danger=self.pad_mute[index],
        )
        self.draw_button(
            self.buttons["desktop_mix_solo"], "Solo",
            active=index in self.solo_pads,
        )
        self.draw_button(self.buttons["desktop_mix_reset"], "Reset")

        self.screen.blit(
            self.small_font.render(
                "Pad FX stay in Advanced on desktop.", True, theme.INK_3
            ),
            (rect.x + 16, rect.y + 392),
        )
        self.buttons["desktop_mix_advanced"] = pygame.Rect(
            rect.x + 16, rect.bottom - 60, rect.width - 32, 44
        )
        self.draw_button(
            self.buttons["desktop_mix_advanced"],
            "Advanced Mixer", icon="sliders",
        )

    def draw_desktop_sampler(self):
        rect = pygame.Rect(
            SIDE_LEFT, GRID_TOP, SIDE_WIDTH, SIDE_BOTTOM - GRID_TOP
        )
        index = self.selected_pad
        filename = self.custom_sample_files[index]
        subtitle = (
            self._sample_display_name(filename)
            if filename
            else "Record, browse, or drop audio onto the window"
        )
        self._panel_shell(rect, "Sampler", subtitle)

        if filename:
            active = min(
                self.pad_layer_index[index], len(self.pad_layers[index]) - 1
            )
            layer = self.pad_layers[index][active]
            self.draw_waveform(
                pygame.Rect(rect.x + 16, rect.y + 86, rect.width - 32, 142),
                layer,
            )
            self.buttons["desktop_sampler_preview"] = pygame.Rect(
                rect.x + 16, rect.y + 250, 98, 42
            )
            self.buttons["desktop_sampler_edit"] = pygame.Rect(
                rect.x + 122, rect.y + 250, 98, 42
            )
            self.buttons["desktop_sampler_clear"] = pygame.Rect(
                rect.x + 228, rect.y + 250, 98, 42
            )
            self.draw_button(
                self.buttons["desktop_sampler_preview"], "Play", icon="play"
            )
            self.draw_button(
                self.buttons["desktop_sampler_edit"], "Edit", icon="waveform"
            )
            self.draw_button(
                self.buttons["desktop_sampler_clear"], "Kit Sound"
            )
        else:
            well = pygame.Rect(rect.x + 16, rect.y + 90, rect.width - 32, 140)
            pygame.draw.rect(self.screen, theme.GROUND, well, border_radius=8)
            pygame.draw.rect(
                self.screen, theme.RULE, well, width=1, border_radius=8
            )
            hint = self.small_font.render(
                "No sample on this pad", True, theme.INK_3
            )
            self.screen.blit(hint, hint.get_rect(center=well.center))

        input_name = self.sample_input_name or "Default input"
        self.screen.blit(
            self.small_font.render("INPUT", True, theme.INK_3),
            (rect.x + 16, rect.y + 322),
        )
        input_text = self.fit_text(
            self.font, input_name, rect.width - 32, theme.INK
        )
        self.screen.blit(input_text, (rect.x + 16, rect.y + 344))

        y = rect.y + 394
        self.buttons["desktop_sampler_record"] = pygame.Rect(
            rect.x + 16, y, 206, 52
        )
        self.buttons["desktop_sampler_browse"] = pygame.Rect(
            rect.x + 230, y, 218, 52
        )
        self.draw_button(
            self.buttons["desktop_sampler_record"], "Record Sample",
            enabled=self.audio_inputs_available, icon="microphone",
        )
        self.draw_button(
            self.buttons["desktop_sampler_browse"],
            "Browse Sounds", icon="folder",
        )

        note = self.small_font.render(
            "Audio files can also be dropped anywhere on the window.",
            True, theme.INK_3,
        )
        self.screen.blit(note, (rect.x + 16, rect.bottom - 42))

    @staticmethod
    def _sample_display_name(value):
        name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
        return name[:44] or "Sample"

    def draw_desktop_notice(self):
        if not getattr(self, "surface_notice", None):
            return
        if time.perf_counter() >= getattr(self, "surface_notice_until", 0.0):
            return
        label = self.small_font.render(self.surface_notice, True, theme.INK)
        box = label.get_rect()
        box.inflate_ip(24, 14)
        box.midbottom = (
            core.WINDOW_SIZE[0] // 2, core.WINDOW_SIZE[1] - 14
        )
        pygame.draw.rect(
            self.screen, theme.PANEL_2, box,
            border_radius=box.height // 2,
        )
        pygame.draw.rect(
            self.screen, theme.RULE, box, width=1,
            border_radius=box.height // 2,
        )
        self.screen.blit(label, label.get_rect(center=box.center))


def main():
    mutex = core.acquire_single_instance()
    if mutex is None:
        print(
            "STARRYPAD is already running. Switch to the open window.",
            file=sys.stderr,
        )
        return
    try:
        app = StarrypadDesktop()
        app.run()
    except Exception as exc:
        try:
            pygame.quit()
        except Exception:
            pass
        print(f"Fatal error: {exc}", file=sys.stderr)
        raise
    finally:
        core.release_single_instance(mutex)


if __name__ == "__main__":
    main()

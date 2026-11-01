"""Unread CC alarms as Home Assistant persistent notifications.

Every unread alarm gets a notification. When the alarm is read elsewhere (app,
CC, the "mark alarms as read" button) the notification disappears. Dismissing
the notification in Home Assistant marks the alarm as read on the CC, which is
what the app does when an alarm is opened.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .alarm_texts import ALARM_TEXTS
from .api import Alarm
from .coordinator import DanfossLinkCoordinator

_LOGGER = logging.getLogger(__name__)

# The app has no Slovak translation; Czech is the closest one it ships.
LANGUAGE_FALLBACKS = {"sk": "cs", "no": "nb", "nn": "nb"}

LABELS = {
    "en": ("Room", "Device", "Dismiss this notification to mark the alarm as read."),
    "sk": ("Miestnosť", "Zariadenie", "Zatvorením tejto notifikácie sa alarm označí ako prečítaný."),
    "cs": ("Místnost", "Zařízení", "Zavřením tohoto oznámení se alarm označí jako přečtený."),
}


def _language(language: str | None) -> str:
    base = (language or "en").split("-")[0].lower()
    return LANGUAGE_FALLBACKS.get(base, base)


def alarm_text(language: str | None, text_id: str | None) -> str | None:
    """Translate an alarm text id like the app; unknown ids are shown as they are."""
    if not text_id:
        return None
    texts = ALARM_TEXTS.get(_language(language), ALARM_TEXTS["en"])
    return texts.get(text_id) or ALARM_TEXTS["en"].get(text_id) or text_id


class AlarmNotifier:
    """Keeps persistent notifications in sync with the CC's unread alarms."""

    def __init__(self, hass: HomeAssistant, coordinator: DanfossLinkCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._prefix = f"danfoss_link_{coordinator.config_entry.entry_id}_alarm_"
        self._shown: set[int] = set()
        # dismissed in HA, not yet confirmed read by the CC
        self._marking: set[int] = set()
        self._unsubscribe: list[Any] = []

    @callback
    def async_start(self) -> None:
        self._unsubscribe.append(
            persistent_notification.async_register_callback(self.hass, self._on_notifications)
        )
        self._unsubscribe.append(self.coordinator.async_add_listener(self._on_update))
        self._on_update()

    @callback
    def async_stop(self) -> None:
        while self._unsubscribe:
            self._unsubscribe.pop()()
        for alarm_id in self._shown:
            persistent_notification.async_dismiss(self.hass, self._prefix + str(alarm_id))
        self._shown.clear()

    @callback
    def _on_update(self) -> None:
        if self.coordinator.data is None or not self.coordinator.last_update_success:
            return
        unread = {a.id: a for a in self.coordinator.data.unread_alarms}
        self._marking &= unread.keys()
        for alarm_id in self._shown - unread.keys():
            self._shown.discard(alarm_id)
            persistent_notification.async_dismiss(self.hass, self._prefix + str(alarm_id))
        for alarm_id, alarm in unread.items():
            if alarm_id in self._shown or alarm_id in self._marking:
                continue
            self._shown.add(alarm_id)
            title, message = self._render(alarm)
            persistent_notification.async_create(
                self.hass, message, title, self._prefix + str(alarm_id)
            )

    def _render(self, alarm: Alarm) -> tuple[str, str]:
        language = self.hass.config.language
        room_label, device_label, dismiss = LABELS.get(_language(language), LABELS["en"])
        data = self.coordinator.data
        title = alarm_text(language, alarm.title_text_id) or alarm.level or "Alarm"
        lines = []
        if description := alarm_text(language, alarm.description_text_id):
            lines.append(description)
        if alarm.room is not None:
            try:
                room = data.rooms[int(alarm.room)].name
            except (ValueError, KeyError):
                room = alarm.room
            lines.append(f"{room_label}: {room}")
        if alarm.node is not None:
            lines.append(f"{device_label}: {alarm.node}")
        if alarm.created:
            lines.append(alarm.created.replace("T", " "))
        lines.append(f"_{dismiss}_")
        return f"{data.info.name}: {title.rstrip('.')}", "\n\n".join(lines)

    @callback
    def _on_notifications(
        self,
        update_type: persistent_notification.UpdateType,
        notifications: dict[str, persistent_notification.Notification],
    ) -> None:
        if update_type is not persistent_notification.UpdateType.REMOVED:
            return
        dismissed = []
        for notification_id in notifications:
            if not notification_id.startswith(self._prefix):
                continue
            try:
                alarm_id = int(notification_id.removeprefix(self._prefix))
            except ValueError:
                continue
            # our own dismissals (alarm already read) are not in _shown any more
            if alarm_id in self._shown:
                self._shown.discard(alarm_id)
                self._marking.add(alarm_id)
                dismissed.append(alarm_id)
        if dismissed:
            self.coordinator.config_entry.async_create_task(
                self.hass, self._mark_read(dismissed), "danfoss_link_mark_alarms_read"
            )

    async def _mark_read(self, alarm_ids: list[int]) -> None:
        try:
            await self.coordinator.async_mark_alarms_read(alarm_ids)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not mark Danfoss Link alarms %s as read: %s", alarm_ids, err)
            self._marking.difference_update(alarm_ids)  # show them again

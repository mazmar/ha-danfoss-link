# Credits

* The protocol was reconstructed from the Danfoss Link Android app 1.3.1
  (Java code and the Trifork MDG Lua modules shipped in the APK).
* **[Ole-JH/ha-danfoss-link](https://github.com/Ole-JH/ha-danfoss-link)** (MIT) –
  an independent integration for the same CC. Ideas for several features came
  from it: alarm notifications, battery / radio / living zone sensors, planned
  vacation, the Wi-Fi signal field (`wifiSignalStrengthDb`), the house mode
  select behaviour and the pairing error messages. The code here is our own.
* Alarm texts in `custom_components/danfoss_link/alarm_texts.py` are the app's
  own translations of the alarm ids the CC reports.

Danfoss and Danfoss Link are trademarks of Danfoss A/S. This project is not
affiliated with Danfoss or Trifork.

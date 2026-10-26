# Starter IT Incident Resolution Advisory Guide
Document ID: GUIDE-IT-2026-V1
Version: 1.2
Status: Approved & Active
Target Services: Wi-Fi, Printer, Email
Operating Systems: Windows 10/11, macOS Sonoma/Ventura

---

## SECTION 1: Wi-Fi and Network Connectivity

### Topic: General Wi-Fi Connection Failure
- **Service:** Wi-Fi
- **Symptom:** Cannot connect to Wi-Fi, no internet, network disconnected
- **OS:** All
- **Step 1:** Confirm Wi-Fi toggle is switched On and Airplane mode is turned Off.
- **Expected Observation:** The list of available company networks appears in network settings.
- **Step 2:** Check whether other team members or your secondary approved mobile device can connect to the SSID.
- **Expected Observation:** Identifies whether the issue is isolated to your computer or office-wide.
- **Step 3:** Disconnect from the company Wi-Fi network (Corp-Secure), wait 10 seconds, and reconnect.
- **Expected Observation:** Device attempts DHCP authentication and requests an IP address.
- **Step 4:** Restart your device if permitted by current IT compliance policy.
- **Expected Observation:** System restarts and reinitializes network interfaces.
- **Step 5:** If the issue continues, record the exact error code or prompt and contact IT Support.
- **Expected Observation:** Documented error code available for ticket creation.

### Topic: Windows Wi-Fi Troubleshooting
- **Service:** Wi-Fi
- **Symptom:** Windows limited connectivity, yellow triangle, DNS failure
- **OS:** Windows
- **Step 1 [Windows]:** Open Settings > Network & Internet > Advanced network settings > Network reset.
- **Expected Observation:** Windows resets network adapters and prompts for a restart within 5 minutes.
- **Step 2 [Windows]:** Open Command Prompt and run `ipconfig /flushdns` followed by `ipconfig /renew`.
- **Expected Observation:** DNS cache is cleared and a fresh DHCP lease IP (10.x.x.x) is assigned.
- **Step 3 [Windows]:** Verify your wireless adapter in Device Manager shows 'This device is working properly'.
- **Expected Observation:** No yellow exclamation mark or error code 43 on the adapter.

### Topic: macOS Wi-Fi Troubleshooting
- **Service:** Wi-Fi
- **Symptom:** macOS Wi-Fi no IP address, self-assigned IP 169.254, dropping connection
- **OS:** macOS
- **Step 1 [macOS]:** Hold the Option key and click the Wi-Fi icon in the menu bar to review BSSID, RSSI, and channel noise.
- **Expected Observation:** RSSI should be stronger than -70 dBm for stable connection.
- **Step 2 [macOS]:** Go to System Settings > Wi-Fi > Details > TCP/IP and click 'Renew DHCP Lease'.
- **Expected Observation:** IPv4 address refreshes from 169.254.x.x to corporate subnet 10.x.x.x.
- **Step 3 [macOS]:** Remove the corporate network from Known Networks, then rejoin entering your SSO credentials.
- **Expected Observation:** Prompts for 802.1X enterprise certificate validation.

---

## SECTION 2: Printer and Printing Problems

### Topic: Printer Offline and Connectivity
- **Service:** Printer
- **Symptom:** Printer offline, printer not responding, cannot discover printer
- **OS:** All
- **Step 1:** Confirm the physical printer is powered on and the front LCD panel displays 'Ready' with no flashing error lights.
- **Expected Observation:** Green power indicator is solid and paper trays are seated.
- **Step 2:** Verify that your computer and the printer are on the same approved corporate network or VLAN.
- **Expected Observation:** Cannot print across guest networks or external VPN tunnels.
- **Step 3:** Open Printers & Scanners on your computer and verify the designated floor printer is selected as Default.
- **Expected Observation:** Default printer name matches the asset tag on your office floor.
- **Step 4:** Send a small single-page test document to verify spooling.
- **Expected Observation:** Printer displays 'Receiving Data' and prints test page.

### Topic: Stuck Print Spooler and Paper Jams
- **Service:** Printer
- **Symptom:** Print job stuck in queue, paper jam, spooler service error
- **OS:** All
- **Step 1:** Open your print queue and cancel only your own pending print jobs; do not delete other users' jobs.
- **Expected Observation:** The queue clears within 30 seconds.
- **Step 2:** Check all physical paper trays, side doors, and output bins for crumpled or stuck sheets.
- **Expected Observation:** Any trapped paper is removed gently along the paper path without tearing.
- **Step 3:** Power-cycle the printer by pressing the power switch, waiting 30 seconds, and turning it back on.
- **Expected Observation:** Printer runs internal diagnostic calibration and displays Ready.

### Topic: Windows Printer Troubleshooting
- **Service:** Printer
- **Symptom:** Windows spooler stopped, error 0x0000011b, printer driver error
- **OS:** Windows
- **Step 1 [Windows]:** Press Win+R, type `services.msc`, locate 'Print Spooler', and click Restart Service.
- **Expected Observation:** Windows spooler service restarts and clears hanging locks.
- **Step 2 [Windows]:** In Windows Settings > Bluetooth & Devices > Printers, select printer and run 'Run the troubleshooter'.
- **Expected Observation:** Windows detects port conflicts and repairs communication.

### Topic: macOS Printer Troubleshooting
- **Service:** Printer
- **Symptom:** macOS printer paused, Filter failed error, CUPS queue halt
- **OS:** macOS
- **Step 1 [macOS]:** Open System Settings > Printers & Scanners, right-click the printer list, and choose 'Reset Printing System'.
- **Expected Observation:** Clears corrupted print queues and daemon state.
- **Step 2 [macOS]:** Re-add the printer using AirPrint or Secure IPP with the corporate printer DNS name.
- **Expected Observation:** macOS downloads verified vendor PPD profile.

---

## SECTION 3: Email and Email Client Problems

### Topic: Email Sync and Connection Issues
- **Service:** Email
- **Symptom:** Cannot send email, cannot receive email, Outlook disconnected, sync error
- **OS:** All
- **Step 1:** Check the company IT service status portal or webmail to verify cloud mail servers are operational.
- **Expected Observation:** Confirms whether mail service is operational for your organization.
- **Step 2:** Open the approved Webmail portal (outlook.office.com or webmail.company.com) in your browser.
- **Expected Observation:** Verifies your account credentials and mailbox access independently of the desktop client.
- **Step 3:** Verify network connection and check the status bar at the bottom of Outlook.
- **Expected Observation:** Status bar displays 'Connected to Microsoft Exchange' instead of 'Disconnected' or 'Need Password'.
- **Step 4:** Check your Outbox folder for any oversized email attachments (exceeding 25MB) that may block the queue.
- **Expected Observation:** Moving or deleting oversized draft restores normal outgoing transmission.
- **Step 5:** Record any exact bounce-back code (e.g. NDR 550 5.1.1) without including passwords or authentication codes.
- **Expected Observation:** Error code saved for escalation.

### Topic: Windows Outlook Troubleshooting
- **Service:** Email
- **Symptom:** Outlook crashes on startup, profile corrupted, OST synchronization error
- **OS:** Windows
- **Step 1 [Windows]:** Launch Outlook in Safe Mode by holding Ctrl while clicking the Outlook shortcut, or run `outlook.exe /safe`.
- **Expected Observation:** Outlook opens with all third-party add-ins disabled.
- **Step 2 [Windows]:** In Control Panel > Mail > Data Files, select your data file and run the Inbox Repair Tool (scanpst.exe).
- **Expected Observation:** Scans OST file for internal corruption and repairs headers.

### Topic: macOS Outlook and Apple Mail Troubleshooting
- **Service:** Email
- **Symptom:** macOS Outlook constantly prompting for credentials, Apple Mail offline
- **OS:** macOS
- **Step 1 [macOS]:** Open Keychain Access, search for 'Exchange' and 'Office', and delete old cached tokens.
- **Expected Observation:** Next launch triggers a fresh Microsoft Modern Authentication SSO prompt.
- **Step 2 [macOS]:** In Outlook menu, click Tools > Accounts > Advanced > Empty Cache.
- **Expected Observation:** Outlook re-synchronizes folders from the Exchange server.

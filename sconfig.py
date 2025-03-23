from gpiozero import Button
from signal import pause
from RPLCD.i2c import CharLCD
import subprocess
import time

# LCD Setup
lcd = CharLCD('PCF8574', 0x27)  # Adjust I2C address if necessary

# Character Set for Input
CHARACTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()_-+= "

# GPIO pin for the button
BUTTON_UP = 6
BUTTON_DOWN = 5
BUTTON_LEFT = 19
BUTTON_RIGHT = 13
BUTTON_ENTER = 26

# global variables
current_menu = "main-ssid"
ssid = ""
password = ""
char_index = 0
current_ssid = 0
ssids = []

def scan_wifi_wpa_cli():
    try:
        # Run the wpa_cli scan_results command with sudo
        result = subprocess.run(
            ["sudo", "wpa_cli", "-i", "wlan0", "scan_results"],
            capture_output=True,
            text=True,
            check=True
        )
        
        # Parse the output to extract SSIDs
        ssids = []
        lines = result.stdout.splitlines()
        
        # Skip the header line (first line) and process the rest
        for line in lines[1:]:
            # Split the line by tabs and extract the SSID (5th column)
            parts = line.split("\t")
            if len(parts) >= 5:  # Ensure the line has enough columns
                ssid = parts[4].strip()  # SSID is in the 5th column
                if ssid:  # Avoid empty SSIDs
                    ssids.append(ssid)
        
        return ssids
    except subprocess.CalledProcessError as e:
        print(f"Error running wpa_cli: {e}")
        return []


# Callback function for button press
def u_button_pressed():
    global current_menu, char_index, current_ssid
    print("Up Button pressed!")
    if current_menu == "main-ssid":
        current_menu = "main-save"
        display_menu()
    elif current_menu == "main-pass":
        current_menu = "main-ssid"
        display_menu()
    elif current_menu == "main-save":
        current_menu = "main-pass"
        display_menu()
    elif current_menu == "ssid" and current_ssid > 0:
        current_ssid = current_ssid - 1
        display_ssid_menu()
    elif current_menu == "pass":
        char_index = (char_index + 1) % len(CHARACTERS)
        update_input_display()
        
def d_button_pressed():
    global current_menu, char_index, current_ssid
    print("Down Button pressed!")
    if current_menu == "main-ssid":
        current_menu = "main-pass"
        display_menu()
    elif current_menu == "main-pass":
        current_menu = "main-save"
        display_menu()
    elif current_menu == "main-save":
        current_menu = "main-ssid"
        display_menu()        
    if current_menu == "ssid" and current_ssid < len(ssids) - 1:
        current_ssid = current_ssid + 1
        display_ssid_menu()        
    elif current_menu == "pass":
        char_index = (char_index - 1) % len(CHARACTERS)
        update_input_display()

        
def l_button_pressed():
    global current_menu, char_index, ssid, password
    print("Left Button pressed!")
    if current_menu == "ssid":
        display_ssid_menu()
    elif current_menu == "pass":
        password = password[:-1]
        update_input_display()
    
def r_button_pressed():
    global current_menu, char_index, ssid, password
    print("Right Button pressed!")
    if current_menu == "ssid":
        display_ssid_menu()
    elif current_menu == "pass":
        password += CHARACTERS[char_index]
        update_input_display()
    
def e_button_pressed():
    global current_menu, ssid, password, char_index, current_ssid
    print("Enter Button pressed!")
    if current_menu == "main-ssid":
        # enter ssid menu
        current_menu = "ssid"
        print("Entering ssid menu")
        lcd.clear()
        update_input_display()
    elif current_menu == "main-pass":
        # enter pass menu
        current_menu = "pass"
        print("Entering pass menu")
        lcd.clear()
        update_input_display()
    elif current_menu == "main-save":
        current_menu = "main-save"
        lcd.clear()
        lcd.write_string("Saving...\n\r")
        print("Saving...\n\r")
        result = subprocess.run(["cp", "/etc/wpa_supplicant/wpa_supplicant.conf", "/etc/wpa_supplicant/wpa_supplicant.conf.bak"])
        if result.returncode == 0:
            print("wpa_supplicant backup succeeded!\n\r")
        else:
            print("wpa_supplicant backup failed!\n\r")
        with open("/etc/wpa_supplicant/wpa_supplicant.conf", "w") as f:
            f.write(f'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\nupdate_config=1\n\nnetwork={{\n    ssid="{ssid}"\n    psk="{password}"\n    key_mgmt=WPA-PSK\n}}\n')
        result = subprocess.run(["wpa_cli", "-i", "wlan0", "reconfigure"])
        if result.returncode == 0:
            print("wpa_cli succeeded!\n\r")
        else:
            print("wpa_cli failed!\n\r")       
        lcd.write_string("Saved.\n\r")
        print("Saved.\n\r")
        time.sleep(1)
        result = subprocess.run(["sudo", "python", "./mainmenu.py"])
        if result.returncode == 0:
            print("mainmenu.py succeeded!\n\r")
        else:
            print("mainmenu.py failed!\n\r")     
        exit()
    elif current_menu == "ssid":
        print("storing ssid = " + ssids[current_ssid])
        current_menu = "main-ssid"
        ssid = ssids[current_ssid]
        lcd.clear()
        display_menu()
    elif current_menu == "pass":
        print("storing pass = " + password + CHARACTERS[char_index])
        current_menu = "main-pass"
        password += CHARACTERS[char_index]
        lcd.clear()
        display_menu()

def display_menu():
    lcd.clear()
    if current_menu == "main-ssid":
        lcd.write_string("1: Set SSID <\n\r2: Set Password\n\r3: Save & Exit")
        print("1: Set SSID <\n\r2: Set Password\n\r3: Save & Exit")
    if current_menu == "main-pass":
        lcd.write_string("1: Set SSID \n\r2: Set Password <\n\r3: Save & Exit")
        print("1: Set SSID\n\r2: Set Password <\n\r3: Save & Exit")
    if current_menu == "main-save":
        lcd.write_string("1: Set SSID \n\r2: Set Password\n\r3: Save & Exit <")
        print("1: Set SSID\n\r2: Set Password\n\r3: Save & Exit <")       

def shorten_strings_in_list(strings):
    shortened_list = []
    for s in strings:
        if len(s) >= 20:
            shortened_list.append(s[:16])
        else:
            shortened_list.append(s)
    return shortened_list

def display_ssid_menu():
    global current_ssid
    spaces = ""
    lcd.clear()
    # truncate ssid if longer than 19 characters
    trunc_ssids = shorten_strings_in_list(ssids)
    #trunc_ssids = ["network 1", "network 2", "network 3", "network 4"] DEBUG TESTING
    print("len(trunc_ssids)")
    print(len(trunc_ssids))
    print("trunc_ssids")
    print(trunc_ssids)
    lcd.write_string("Select Network:\n\r")
    if current_ssid == 0:
        for x in range(19 - len(trunc_ssids[current_ssid])):
            spaces = spaces + " "
            print(x)
        lcd.write_string(trunc_ssids[current_ssid] + spaces + "<\n\r")
        print(trunc_ssids[current_ssid] + spaces + "<\n\r")
        if len(trunc_ssids) > 1:
            lcd.write_string(trunc_ssids[current_ssid + 1] + "\n\r")
            print(trunc_ssids[current_ssid + 1] + "\n\r")
        if len(trunc_ssids) > 2:
            lcd.write_string(trunc_ssids[current_ssid + 2] + "\n\r")
            print(trunc_ssids[current_ssid + 2] + "\n\r")
    elif current_ssid < len(trunc_ssids) - 1:
        lcd.write_string(trunc_ssids[current_ssid - 1] + "\n\r")
        print(trunc_ssids[current_ssid - 1] + "\n\r")   
        for x in range(19 - len(trunc_ssids[current_ssid])):
            spaces = spaces + " "
            print(x)
        lcd.write_string(trunc_ssids[current_ssid] + spaces + "<\n\r")
        print(trunc_ssids[current_ssid] + spaces + "<\n\r")
        if len(trunc_ssids) > 2:
            lcd.write_string(trunc_ssids[current_ssid + 1] + "\n\r")
            print(trunc_ssids[current_ssid + 1] + "\n\r")      
    elif current_ssid == len(trunc_ssids) - 1:
        lcd.write_string(ssids[current_ssid - 2] + "\n\r") 
        print(trunc_ssids[current_ssid - 2] + "\n\r")   
        lcd.write_string(trunc_ssids[current_ssid - 1] + "\n\r")
        print(trunc_ssids[current_ssid - 1] + "\n\r")
        for x in range(19 - len(trunc_ssids[current_ssid])):
            spaces = spaces + " "
            print(x)
        lcd.write_string(trunc_ssids[current_ssid] + spaces + "<\n\r")
        print(trunc_ssids[current_ssid] + spaces + "<\n\r")     

def update_input_display():
    global current_menu, ssid, password, char_index
    lcd.clear()
    print("updating input display menu :" + current_menu)
    if current_menu == "ssid":
        display_ssid_menu()
    elif current_menu == "pass":
        lcd.write_string("Enter Password:\n\r" + "*" * len(password) + CHARACTERS[char_index])
        print("Enter Password:\n\r" + "*" * len(password) + CHARACTERS[char_index])
        
# Setup button
u_button = Button(BUTTON_UP)
d_button = Button(BUTTON_DOWN)
l_button = Button(BUTTON_LEFT)
r_button = Button(BUTTON_RIGHT)
e_button = Button(BUTTON_ENTER)

# Assign callback to button press event
u_button.when_pressed = u_button_pressed
d_button.when_pressed = d_button_pressed
l_button.when_pressed = l_button_pressed
r_button.when_pressed = r_button_pressed
e_button.when_pressed = e_button_pressed

try:
    ssids = scan_wifi_wpa_cli()
    #ssids = ["network 1", "network 2", "network 3", "network 4"] DEBUG TESTING
    print("len(ssids)")
    print(len(ssids))
    print("ssids")
    print(ssids)
    display_menu()
    print("Waiting for button press...")
    pause()  # Keep the program running
except KeyboardInterrupt:
    lcd.clear()



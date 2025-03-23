from gpiozero import Button
from signal import pause
from RPLCD.i2c import CharLCD
import subprocess

# LCD Setup
lcd = CharLCD('PCF8574', 0x27)  # Adjust I2C address if necessary

# Character Set for Input
CHARACTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()_-+= "

# GPIO pin for the button
BUTTON_UP = 6
BUTTON_DOWN = 5
BUTTON_ENTER = 26

# global variables
current_menu = "main-scrape"

# Callback function for button press
def u_button_pressed():
    global current_menu
    print("Up Button pressed!")
    if current_menu == "main-scrape":
        current_menu = "main-skihill"
        display_menu()
    elif current_menu == "main-config":
        current_menu = "main-scrape"
        display_menu()
    elif current_menu == "main-skihill":
        current_menu = "main-config"
        display_menu()
        
def d_button_pressed():
    global current_menu
    print("Down Button pressed!")
    if current_menu == "main-scrape":
        current_menu = "main-config"
        display_menu()
    elif current_menu == "main-config":
        current_menu = "main-skihill"
        display_menu()
    elif current_menu == "main-skihill":
        current_menu = "main-scrape"
        display_menu()

def e_button_pressed():
    global current_menu
    print("Enter Button pressed!")
    if current_menu == "main-scrape":
        # run snowscraper.py
        print("Running snowscraper.py")
        lcd.clear()
        result = subprocess.run(["python", "./snowscraper.py"])
        if result.returncode == 0:
            print("python ./snowscraper.py succeeded!\n\r")
        else:
            print("python ./snowscraper.py failed!\n\r")
        exit()
            
    elif current_menu == "main-config":
        # run sconfig.py
        print("Running sconfig.py")
        lcd.clear()
        result = subprocess.run(["python", "./sconfig.py"])
        if result.returncode == 0:
            print("python ./sconfig.py succeeded!\n\r")
        else:
            print("python ./sconfig.py failed!\n\r")
        exit()
        
    elif current_menu == "main-skihill":
        # run skimenu.py
        print("Running skimenu.py")
        lcd.clear()
        result = subprocess.run(["python", "./skimenu.py"])
        if result.returncode == 0:
            print("python ./skimenu.py succeeded!\n\r")
        else:
            print("python ./skimenu.py failed!\n\r")
        exit()

def display_menu():
    lcd.clear()
    if current_menu == "main-scrape":
        lcd.write_string("1: Run SnowScraper <\n\r2: Configure WiFi\n\r3: Select Mountain\n\r")
        print("1: Run SnowScraper <\n\r2: Configure WiFi\n\r 3: Select Mountain\n\r")
    elif current_menu == "main-config":
        lcd.write_string("1: Run SnowScraper\n\r2: Configure WiFi  <\n\r3: Select Mountain\n\r")
        print("1: Run SnowScraper\n\r2: Configure WiFi  <\n\r 3: Select Mountain\n\r")
    elif current_menu == "main-skihill":
        lcd.write_string("1: Run SnowScraper \n\r2: Configure WiFi\n\r3: Select Mountain <\n\r")
        print("1: Run SnowScraper\n\r2: Configure WiFi\n\r3: Select Mountain <\n\r")
  
# Setup button
u_button = Button(BUTTON_UP)
d_button = Button(BUTTON_DOWN)
e_button = Button(BUTTON_ENTER)

# Assign callback to button press event
u_button.when_pressed = u_button_pressed
d_button.when_pressed = d_button_pressed
e_button.when_pressed = e_button_pressed

try:
    display_menu()
    print("Waiting for button press...")
    pause()  # Keep the program running
except KeyboardInterrupt:
    lcd.clear()

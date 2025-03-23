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
BUTTON_ENTER = 26

# global variables
current_menu = 0

# create skiHill class
class skiHill:
  def __init__(self, name, url, newSnow, weekSnow, baseSnow):
    self.name = name
    self.url = url
    self.newSnow = newSnow
    self.weekSnow = weekSnow
    self.baseSnow = baseSnow

# define all ski hills
sunPeaks = skiHill(name = "Sun Peaks", url = "https://www.sunpeaksresort.com/ski-ride/weather-conditions-cams/weather-snow-report", newSnow = 0, weekSnow = 0, baseSnow = 0)
silverStar = skiHill(name = "SilverStar", url = "https://www.skisilverstar.com/the-mountain/weather-conditions/snow-report-forecast", newSnow = 0, weekSnow = 0, baseSnow = 0)
bigWhite = skiHill(name = "Big White", url = "https://www.bigwhite.com/mountain-conditions/daily-snow-report", newSnow = 0, weekSnow = 0, baseSnow = 0)
whistler = skiHill(name = "Whistler", url = "https://www.whistlerblackcomb.com/the-mountain/mountain-conditions/snow-and-weather-report.aspx", newSnow = 0, weekSnow = 0, baseSnow = 0)
revelstoke = skiHill(name = "Revelstoke", url = "https://www.revelstokemountainresort.com/mountain/conditions/snow-report/", newSnow = 0, weekSnow = 0, baseSnow = 0)
kickingHorse = skiHill(name = "Kicking Horse", url = "https://kickinghorseresort.com/conditions/snow-report/", newSnow = 0, weekSnow = 0, baseSnow = 0)
lakeLouise = skiHill(name = "Lake Louise", url = "https://www.skilouise.com/snow-conditions/", newSnow = 0, weekSnow = 0, baseSnow = 0)
banffSunshine = skiHill(name = "Banff Sunshine", url = "https://www.skibanff.com/conditions/", newSnow = 0, weekSnow = 0, baseSnow = 0)
redMountain = skiHill(name = "Red Mountian", url = "https://www.redresort.com/report/", newSnow = 0, weekSnow = 0, baseSnow = 0)
whiteWater = skiHill(name = "WhiteWater", url = "https://skiwhitewater.com/conditions/", newSnow = 0, weekSnow = 0, baseSnow = 0)

#create list of ski hills
skiHills = [sunPeaks, silverStar, bigWhite, whistler, revelstoke, kickingHorse, lakeLouise, banffSunshine, redMountain, whiteWater]

# Callback function for button press
def u_button_pressed():
    global current_menu
    print("Up Button pressed!")
    if current_menu == 0:
        display_menu()
    else:
        current_menu = current_menu - 1
        display_menu()  
        
def d_button_pressed():
    global current_menu
    print("Down Button pressed!")
    if current_menu == len(skiHills) - 1:
        display_menu()
    else:
        current_menu = current_menu + 1
        display_menu()

def e_button_pressed():
    global current_menu
    print("Enter Button pressed!")
    print("Writing " + skiHills[current_menu].name + " to skihill.conf\n\r")
    with open("./skihill.conf", "w") as f:
            f.write(f'{current_menu}')
    lcd.clear()
    lcd.write_string("Saved.\n\r")
    print("Saved.\n\r")
    time.sleep(1)
    result = subprocess.run(["sudo", "python", "./mainmenu.py"])
    if result.returncode == 0:
      print("mainmenu.py succeeded!\n\r")
    else:
      print("mainmenu.py failed!\n\r")     
    exit()

def display_menu():
    spaces = ""
    lcd.clear()
    lcd.write_string("Select Mountain:\n\r")
    if current_menu == 0:
        for x in range(19 - len(skiHills[current_menu].name)):
            spaces = spaces + " "
            print(x)
        lcd.write_string(skiHills[current_menu].name + spaces + "<\n\r")
        print(skiHills[current_menu].name + spaces + "<\n\r")   
        lcd.write_string(skiHills[current_menu + 1].name + "\n\r")
        print(skiHills[current_menu + 1].name + "\n\r")
        lcd.write_string(skiHills[current_menu + 2].name + "\n\r")
        print(skiHills[current_menu + 2].name + "\n\r")
    elif current_menu < len(skiHills) - 1:
        lcd.write_string(skiHills[current_menu - 1].name + "\n\r")
        print(skiHills[current_menu - 1].name + "\n\r")   
        for x in range(19 - len(skiHills[current_menu].name)):
            spaces = spaces + " "
            print(x)
        lcd.write_string(skiHills[current_menu].name + spaces + "<\n\r")
        print(skiHills[current_menu].name + spaces + "<\n\r")   
        lcd.write_string(skiHills[current_menu + 1].name + "\n\r")
        print(skiHills[current_menu + 1].name + "\n\r")      
    elif current_menu == len(skiHills) - 1:
        lcd.write_string(skiHills[current_menu - 2].name + "\n\r") 
        print(skiHills[current_menu - 2].name + "\n\r")   
        lcd.write_string(skiHills[current_menu - 1].name + "\n\r")
        print(skiHills[current_menu - 1].name + "\n\r")
        for x in range(19 - len(skiHills[current_menu].name)):
            spaces = spaces + " "
            print(x)
        lcd.write_string(skiHills[current_menu].name + spaces + "<\n\r")
        print(skiHills[current_menu].name + spaces + "<\n\r")     
        
        
  
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

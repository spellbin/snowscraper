import requests
from bs4 import BeautifulSoup
from RPLCD.i2c import CharLCD
import json
import re
import board
import neopixel
import time

# WS2812B LED Setup
LED_PIN = board.D18  # GPIO18
LED_COUNT = 7  # Number of LEDs
BRIGHTNESS = 0.5  # Brightness (0.0 to 1.0)

# Initialize the NeoPixel
pixels = neopixel.NeoPixel(LED_PIN, LED_COUNT, brightness=BRIGHTNESS, auto_write=False)

# Define the list of RGB values from blue -> red
rgb_values = [
    (0, 0, 139),
    (13, 0, 131),
    (26, 0, 123),
    (39, 0, 115),
    (52, 0, 107),
    (65, 0, 99),
    (78, 0, 91),
    (91, 0, 83),
    (104, 0, 75),
    (117, 0, 67),
    (130, 0, 59),
    (143, 0, 51),
    (156, 0, 43),
    (169, 0, 35),
    (182, 0, 27),
    (195, 0, 19),
    (208, 0, 11),
    (221, 0, 3),
    (234, 0, 0),
    (255, 0, 0)
]

# Initalize the LCD
lcd = CharLCD('PCF8574', 0x27, port=1, cols=20, rows=4, dotsize=8, charmap='A02', auto_linebreaks=True, backlight_enabled=True)  # Adjust I2C address if necessary

# create skiHill class
class skiHill:
  def __init__(self, name, url, newSnow, weekSnow, baseSnow):
    self.name = name
    self.url = url
    self.newSnow = newSnow
    self.weekSnow = weekSnow
    self.baseSnow = baseSnow

  def getSnow(self):
 
    if self.name == "Sun Peaks":
        print("Hello my name is " + self.name)
        values = []
        # Initialize Beautiful Soup
        response = requests.get(self.url)
        soup = BeautifulSoup(response.text, 'html.parser')
        # Find the element containing the snow amount
        # You'll need to inspect the website to find the correct element
        value = soup.find('span', class_='snow-new').text.strip()
        value = int(value)
        self.newSnow = int(value)
        value = soup.find('span', class_='snow-7').text.strip()
        self.weekSnow = int(value)
            # Find the element containing the snow amount
        # You'll need to inspect the website to find the correct element
        html_array = soup.find_all('span', class_='value_switch value_cm')

        for html_string in html_array:
            # Parse the HTML string using BeautifulSoup
            soup2 = BeautifulSoup(str(html_string), 'html.parser')
    
            # Find the <span> element with the specified class
            span_element = soup2.find('span', class_='value_switch value_cm')
    
            # Extract the text content and strip any leading/trailing whitespace
            value = span_element.text.strip()
    
            # Convert the value to an integer (optional, if you want to work with numbers)
            value = int(value)
            # Filter out values you don't want (e.g., ignore 0)
            #    if value != 0:
            values.append(value)

        self.baseSnow = int(values[2])

    if self.name == "Whistler":
      print("Hello my name is " + self.name)
      # Send a GET request to the webpage
      response = requests.get(self.url)

      # Check if the request was successful
      if response.status_code == 200:
        # Parse the HTML content using BeautifulSoup
        soup = BeautifulSoup(response.content, 'html.parser')
    
        # Find the <script> tag containing the snow report data
        script_tag = soup.find('script', string=re.compile(r'FR\.snowReportData\s*='))
    
        if script_tag:
          # Extract the JavaScript content
          script_content = script_tag.string
        
          # Use regex to extract the JSON part of the script
          match = re.search(r'FR\.snowReportData\s*=\s*({.*?});', script_content, re.DOTALL)
        
          if match:
            # Extract the JSON string
            json_data = match.group(1)
            
            # Parse the JSON data
            snow_report_data = json.loads(json_data)
            
            # Extract the 12-hour snowfall value in centimeters
            overnight_snowfall_cm = snow_report_data.get('OvernightSnowfall', {}).get('Centimeters')
            self.newSnow = int(overnight_snowfall_cm)
            if overnight_snowfall_cm:
                print(f"12-Hour Snowfall: {overnight_snowfall_cm} cm")
            else:
                print("12-Hour Snowfall data not found in the JSON.")
          else:
                print("Failed to extract JSON data from the script tag.")

          # Use regex to extract the JSON part of the script
          match = re.search(r'FR\.snowReportData\s*=\s*({.*?});', script_content, re.DOTALL)
        
          if match:
            # Extract the JSON string
            json_data = match.group(1)
            
            # Parse the JSON data
            snow_report_data = json.loads(json_data)
            
            # Extract the 7-day snowfall value in centimeters
            seven_day_snowfall_cm = snow_report_data.get('SevenDaySnowfall', {}).get('Centimeters')
            self.weekSnow = int(seven_day_snowfall_cm)
            if seven_day_snowfall_cm:
                print(f"7-Day Snowfall: {seven_day_snowfall_cm} cm")
            else:
                print("7-Day Snowfall data not found in the JSON.")
          else:
            print("Failed to extract JSON data from the script tag.")

          # Use regex to extract the JSON part of the script
          match = re.search(r'FR\.snowReportData\s*=\s*({.*?});', script_content, re.DOTALL)
        
          if match:
            # Extract the JSON string
            json_data = match.group(1)
            
            # Parse the JSON data
            snow_report_data = json.loads(json_data)
            
            # Extract the base depth value in centimeters
            base_depth_cm = snow_report_data.get('BaseDepth', {}).get('Centimeters')
            self.baseSnow = int(base_depth_cm)
            if base_depth_cm:
                print(f"Base Depth: {base_depth_cm} cm")
            else:
                print("Base Depth data not found in the JSON.")
          else:
                print("Failed to extract JSON data from the script tag.")
        else:
            print("Script tag containing snow report data not found.")
      else:
        print(f"Failed to retrieve the webpage. Status code: {response.status_code}")

    if self.name == "Big White":
        print("Hello my name is " + self.name)
        
        # Initialize Beautiful Soup
        response = requests.get(self.url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find all <span> elements with the class "bigger-font"
        span_elements = soup.find_all('span', class_='bigger-font')

        # Check if any elements were found
        if span_elements:
            print(f"Found {len(span_elements)} elements:")
            for index, span in enumerate(span_elements, start=1):
                # Extract the text content and replace `&nbsp;` with a space
                text = span.text.replace('&nbsp;', ' ')
                if index == 5:
                    self.newSnow = text
                elif index == 7:
                    self.baseSnow = text
                print(f"Element {index}: {text}")
                
        big_font_elements = soup.find_all(class_='big-font')

        # Check if any elements were found
        if big_font_elements:
            print(f"Found {len(big_font_elements)} elements:")
            for index, element in enumerate(big_font_elements, start=1):
                # Extract the text content and replace `&nbsp;` with a space
                text = element.text.replace('&nbsp;', ' ')
                if index == 2:
                    self.weekSnow = text
                print(f"Element {index}: {text}")
                

        else:
            print("No elements found.")
            
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




# Function to set LED color based on snow amount
def set_led_color(snow_amount):
    if snow_amount == 0:
        pixels.fill((255, 255, 255))
        pixels.show()
    elif snow_amount > 20:
        for x in range(20):
          r, g, b = rgb_values[x]
          pixels.fill((r, g, b))
          pixels.show()
          if x > 10:
            delay = 1 / x
            time.sleep(delay)
          else:
            time.sleep(0.1)
    else:
        for x in range(snow_amount):
          r, g, b = rgb_values[x]
          pixels.fill((r, g, b))
          pixels.show()
          if x > 10:
            delay = 1 / x
            time.sleep(delay)
          else:
            time.sleep(0.1)
          #time.sleep(0.03)
       

if __name__ == "__main__":
    try:
        #open skihill.conf to determine ski hill
        # Open the file in read mode
        with open('/home/snowdev/skihill.conf', 'r') as file:
          # Read the first character
          first_char = file.read(1)
    
        # Convert the character to an integer using ord()
        if first_char:  # Check if the file is not empty
          mountain = int(first_char)
        else:
          mountain = 0  # Handle the case where the file is empty

        # get the snow data
        
        skiHills[mountain].getSnow()
      
        # Display on LCD

        lcd.clear()
        resort_name = skiHills[mountain].name
        message = f"      {resort_name}\n\r"
        lcd.write_string(message)
        print(message)
        snow_amount = skiHills[mountain].newSnow
        message = f"New Snow:      {snow_amount}cm\n\r"
        print(message)
        lcd.write_string(message)
        snow_amount = skiHills[mountain].weekSnow
        message = f"7 Day Snow:    {snow_amount}cm\n\r"
        print(message)
        lcd.write_string(message)       
        snow_amount = skiHills[mountain].baseSnow
        message = f"Alpine Base:   {snow_amount}cm"
        print(message)
        lcd.write_string(message)

        # Set LED color
        set_led_color(skiHills[mountain].newSnow)

        # demo code
        '''
        for x in range(25):
          lcd.clear()
          set_led_color(x)
          resort_name = skiHills[mountain].name
          message = f"{resort_name}\n\r"
          print(message)
          lcd.write_string(message)      
          message = f"New Snow:      {x}cm\n\r"
          print(message)
          lcd.write_string(message)

          snow_amount = x + 25
          message = f"7 Day Snow:    {snow_amount}cm\n\r"
          print(message)
          lcd.write_string(message)       
    
          snow_amount = x + 185
          message = f"Alpine Base:   {snow_amount}cm"
          print(message)
          lcd.write_string(message)
          time.sleep(5)
          '''                                
    except Exception as e:
        print(f"Error: {e}")
    

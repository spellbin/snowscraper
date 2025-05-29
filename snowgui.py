import time
import threading
import json
import os
import spidev
import subprocess
import requests
import re
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from luma.core.interface.serial import spi
from luma.lcd.device import ili9341

CALIBRATION_FILE = "touch_calibration.json"
HEARTBEAT_FILE = "heartbeat.txt"
HEARTBEAT_INTERVAL = 30  # seconds

# --- Heartbeat function to detect hangups
def heartbeat():
    while True:
        with open(HEARTBEAT_FILE, 'w') as f:
            f.write(str(time.time()))
        time.sleep(HEARTBEAT_INTERVAL)

# --- Display and SPI Setup ---
serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
device = ili9341(serial_interface=serial, width=320, height=240, rotate=0)

# --- Create skiHill class ---
class skiHill:
  def __init__(self, name, url, newSnow, weekSnow, baseSnow):
    self.name = name
    self.url = url
    self.newSnow = newSnow
    self.weekSnow = weekSnow
    self.baseSnow = baseSnow

  def getSnow(self):

    if self.name == "Red Mountain":

      print("Hello my name is " + self.name)

      # 1. Fetch the API response as raw text
      print("Fetching data from Red Resort API...")
      response = requests.get(self.url)
      response.raise_for_status()
      response_text = response.text
      print("\nRaw API Response:")
      print(response_text[:500] + "...")  # Print first 500 chars to show sample
        
      # 2. Define patterns to search for each metric
      patterns = {
          '24_hour': r'"Metric24hours":\s*(\d+)',
          '7_day': r'"Metric7days":\s*(\d+)',
          'base_depth': r'"MetricAlpineSnowDepth":\s*(\d+)'
      }
        
      # 3. Search and extract values
      snow_metrics = {}
      for metric, pattern in patterns.items():
          match = re.search(pattern, response_text)
          if match:
              snow_metrics[metric] = int(match.group(1))
          else:
              print(f"Warning: Could not find {metric} in response")
              snow_metrics[metric] = 0  # Default value if not found
        
      # 4. Print the extracted values
      print("\nExtracted Snow Metrics:")
      print(f"24-Hour Snowfall: {snow_metrics['24_hour']} cm")
      print(f"7-Day Snowfall:   {snow_metrics['7_day']} cm")
      print(f"Base Snow Depth:  {snow_metrics['base_depth']} cm")
      self.newSnow = snow_metrics['24_hour']
      self.weekSnow = snow_metrics['7_day']
      self.baseSnow = snow_metrics['base_depth']
        
    if self.name == "Kicking Horse":

      print("Hello my name is " + self.name)
      
      response = requests.get(
              SPLASH_URL,
              params={
                  "url": self.url,
                  "wait": WAIT_TIME,
                  "timeout": 30,
              }
      )
      response.raise_for_status()

      soup = BeautifulSoup(response.text, 'html.parser')
      element1 = soup.find('div', id='snowReportNewSnowFallOvernight')
      element2 = soup.find('div', id='snowReportNewSnowFallPack')
      element3 = soup.find('div', id='snowReportNewSnowFall7days')
      
      self.newSnow = int(''.join(filter(str.isdigit, element1.get_text(strip=True))))
      self.baseSnow = int(''.join(filter(str.isdigit, element2.get_text(strip=True))))
      self.weekSnow = int(''.join(filter(str.isdigit, element3.get_text(strip=True))))

    if self.name == "Banff Sunshine":
        print("Hello my name is " + self.name)
        
        response = requests.get(self.url)

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
    
            # Find the Banff Sunshine (Sunshine Village) section
            sunshine_section = soup.find("div", class_="sv-print")
    
            if sunshine_section:
            # Locate the snowfall table
                table = sunshine_section.find("table", class_="stats")
        
                if table:
                # Extract all table cells (td) in the first row of tbody
                    cells = table.find("tbody").find_all("td")
            
                    if len(cells) >= 5:  # Ensure all expected columns exist
                        overnight = cells[0].get_text(strip=True).split("/")[0].strip().split()[0]
                        seven_day = cells[2].get_text(strip=True).split("/")[0].strip().split()[0]
                        base = cells[3].get_text(strip=True).split("/")[0].strip().split()[0]                      
                        self.newSnow = int(overnight)
                        self.weekSnow = int(seven_day)     
                        self.baseSnow = int(base)
                        print(f"Banff Sunshine Snow Report:")
                        print(f"- Overnight Snowfall: {overnight}")
                        print(f"- 7-Day Snowfall: {seven_day}")
                        print(f"- Base Depth: {base}")
                    else:
                        print("Snowfall data columns missing.")
                else:
                    print("Snowfall table not found.")
            else:
                print("Banff Sunshine section not found.")
        else:
            print(f"Failed to fetch the page. HTTP Status: {response.status_code}")

    if self.name == "Lake Louise":
      
        print("Hello my name is " + self.name)
        response = requests.get(self.url)

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
    
            # Find the Lake Louise section
            sunshine_section = soup.find("div", class_="ll-print")
    
            if sunshine_section:
            # Locate the snowfall table
                table = sunshine_section.find("table", class_="stats")
        
                if table:
                # Extract all table cells (td) in the first row of tbody
                    cells = table.find("tbody").find_all("td")
            
                    if len(cells) >= 5:  # Ensure all expected columns exist
                        overnight = cells[0].get_text(strip=True).split("/")[0].strip().split()[0]
                        seven_day = cells[2].get_text(strip=True).split("/")[0].strip().split()[0]
                        base = cells[3].get_text(strip=True).split("/")[0].strip().split()[0]                      
                        self.newSnow = int(overnight)
                        self.weekSnow = int(seven_day)     
                        self.baseSnow = int(base)
                        print(f"Lake Louise Snow Report:")
                        print(f"- Overnight Snowfall: {overnight}")
                        print(f"- 7-Day Snowfall: {seven_day}")
                        print(f"- Base Depth: {base}")
                    else:
                        print("Snowfall data columns missing.")
                else:
                    print("Snowfall table not found.")
            else:
                print("Lake Louise section not found.")
        else:
            print(f"Failed to fetch the page. HTTP Status: {response.status_code}")
            
    if self.name == "Revelstoke":
        print("Hello my name is " + self.name)
        # Fetch the webpage
        response = requests.get(self.url)
        response.raise_for_status()  # Raise exception for bad status codes
    
        # Parse HTML content
        soup = BeautifulSoup(response.text, 'html.parser')
    
        # Find the snow report section
        section = soup.find('section', class_='snow-report__section')
        if not section:
            raise ValueError("Snow report section not found")
    
        # Extract New Snow
        new_snow_div = section.find('div', class_='snow-report__new')
        value = new_snow_div.find('span', class_='value').text.strip() if new_snow_div else "N/A"
        self.newSnow = int(value)
    
        # Extract amounts from the amounts container
        amounts_container = section.find('div', class_='snow-report__amounts')
        amounts = amounts_container.find_all('div', class_='snow-report__amount') if amounts_container else []

        # Process each amount entry
        for amount in amounts:
            title = amount.find('h2', class_='snow-report__title')
            if not title:
                continue
            
            title_text = title.text.strip()
            value_span = amount.find('span', class_='value')
            value = value_span.text.strip() if value_span else "N/A"
        
            if title_text == "Base Depth":
                self.baseSnow = int(value)
            elif title_text == "7 days":
                self.weekSnow = int(value)
    
 
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
  
# --- Get available SSIDs
def get_available_ssids():
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

def reconfigure_wifi():
    try:
        result = subprocess.run(["wpa_cli", "-i", "wlan0", "reconfigure"])
        if result.returncode == 0:
            print("[WiFi] wpa_cli reconfigure succeeded!")
        else:
            print("[WiFi] wpa_cli reconfigure failed!")
    except Exception as e:
        print(f"[WiFi] Error running wpa_cli: {e}")

# --- Touch Controller ---
class XPT2046:
    def __init__(self, spi_bus=0, spi_device=1):
        self.spi = spidev.SpiDev()
        self.spi.open(spi_bus, spi_device)
        self.spi.max_speed_hz = 500000
        self.spi.mode = 0b00

    def _read_channel(self, cmd):
        response = self.spi.xfer2([cmd, 0x00, 0x00])
        value = ((response[1] << 8) | response[2]) >> 4
        return value

    def read_touch(self, samples=5, tolerance=50):
        readings = []
        for _ in range(samples):
            raw_y = self._read_channel(0xD0)
            raw_x = self._read_channel(0x90)
            if 100 < raw_x < 4000 and 100 < raw_y < 4000:
                readings.append((raw_x, raw_y))
            time.sleep(0.01)

        if len(readings) < 3:
            return None

        xs, ys = zip(*readings)
        if max(xs) - min(xs) > tolerance or max(ys) - min(ys) > tolerance:
            return None

        avg_x = sum(xs) // len(xs)
        avg_y = sum(ys) // len(ys)
        return (avg_x, avg_y)

    def close(self):
        self.spi.close()

# --- Calibration ---
class TouchCalibrator:
    def __init__(self):
        self.x_min = 0
        self.x_max = 4095
        self.y_min = 0
        self.y_max = 4095

    def calibrate(self, touch_reader):
        points = [("Top-Left", (20, 20)), ("Bottom-Right", (300, 220))]
        raw_points = []

        img = Image.new("RGB", (device.width, device.height), "black")
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()

        for label, pos in points:
            draw.rectangle((0, 0, device.width, device.height), fill="black")
            draw.ellipse((pos[0] - 5, pos[1] - 5, pos[0] + 5, pos[1] + 5), fill="red")
            draw.text((10, 10), f"Touch {label} corner", font=font, fill="white")
            device.display(img)

            print(f"Touch the {label} corner...")
            while True:
                coord = touch_reader.read_touch()
                if coord:
                    print(f"{label} touch detected: {coord}")
                    raw_points.append(coord)
                    time.sleep(1)
                    break

        (rx0, ry0), (rx1, ry1) = raw_points
        if rx0 == rx1 or ry0 == ry1:
            raise ValueError("Calibration failed.")
        self.x_min, self.x_max = sorted((rx0, rx1))
        self.y_min, self.y_max = sorted((ry0, ry1))
        self.save()

        # Clear screen after calibration
        img = Image.new("RGB", (device.width, device.height), "black")
        draw = ImageDraw.Draw(img)
        draw.text((10, 10), "Calibration complete", fill="white", font=font)
        device.display(img)
        time.sleep(1)

    def map_raw_to_screen(self, x, y):
        sx = int((x - self.x_min) * device.width / (self.x_max - self.x_min))
        sy = int((y - self.y_min) * device.height / (self.y_max - self.y_min))
        sx = device.width - 1 - sx
        sy = device.height - 1 - sy
        return (max(0, min(device.width - 1, sx)),
                max(0, min(device.height - 1, sy)))

    def save(self):
        with open(CALIBRATION_FILE, "w") as f:
            json.dump({
                "x_min": self.x_min, "x_max": self.x_max,
                "y_min": self.y_min, "y_max": self.y_max
            }, f)

    def load(self):
        if not os.path.exists(CALIBRATION_FILE):
            return False
        with open(CALIBRATION_FILE, "r") as f:
            data = json.load(f)
        self.x_min = data["x_min"]
        self.x_max = data["x_max"]
        self.y_min = data["y_min"]
        self.y_max = data["y_max"]
        return True

# --- Button ---
class Button:
    def __init__(self, x1, y1, x2, y2, label, callback, visible=False):
        self.x1, self.y1 = x1, y1
        self.x2, self.y2 = x2, y2
        self.label = label
        self.callback = callback
        self.visible = visible

    def contains(self, x, y):
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2

    def draw(self, draw_obj):
        if not self.visible:
            return
        draw_obj.rectangle([self.x1, self.y1, self.x2, self.y2], outline="white", fill="gray")
        font = ImageFont.load_default()
        draw_obj.text((self.x1 + 5, self.y1 + 5), self.label, fill="black", font=font)

    def on_press(self):
        print(f"[BUTTON] {self.label}")
        self.callback()

# --- Base Screen ---
class Screen:
    def __init__(self):
        self.buttons = []

    def add_button(self, button):
        self.buttons.append(button)

    def draw(self, draw_obj):
        for btn in self.buttons:
            btn.draw(draw_obj)

    def handle_touch(self, x, y):
        for btn in self.buttons:
            if btn.contains(x, y):
                btn.on_press()

# --- Keyboard Screen ---                
class KeyboardScreen(Screen):
    def __init__(self, prompt, on_submit, screen_manager):
        super().__init__()
        self.prompt = prompt
        self.on_submit = on_submit
        self.screen_manager = screen_manager
        self.input_text = ""
        self.mode = "letters"  # or 'symbols'
        self.shift = False
        self._build_keys()

    def _build_keys(self):
        self.buttons.clear()

        if self.mode == "letters":
            rows = [
                list("QWERTYUIOP"),
                list("ASDFGHJKL"),
                list("ZXCVBNM"),
            ]
        else:
            rows = [
                list("1234567890"),
                list("!@#$%^&*()"),
                list("-_=+.,?/")
            ]

        x_start = 10
        y_start = 60
        key_w = 28
        key_h = 28
        spacing = 4

        for row_index, row in enumerate(rows):
            for col_index, char in enumerate(row):
                label = char.upper() if self.shift else char.lower()
                x = x_start + col_index * (key_w + spacing)
                y = y_start + row_index * (key_h + spacing)
                char_label = label  # make a stable copy for lambda
                self.add_button(Button(x, y, x + key_w, y + key_h, char_label,
                                       lambda c=char_label: self._append_char(c), visible=True))
 

        # Toggle [123]/[ABC]
        toggle_label = "[123]" if self.mode == "letters" else "[ABC]"
        self.add_button(Button(10, 160, 65, 190, toggle_label, self._toggle_mode, visible=True))

        # Shift
        shift_label = "[↑]" if not self.shift else "[↓]"
        self.add_button(Button(70, 160, 125, 190, shift_label, self._toggle_shift, visible=True))

        # Space, Backspace, Enter
        self.add_button(Button(130, 160, 220, 190, "Space", lambda: self._append_char(" "), visible=True))
        self.add_button(Button(225, 160, 270, 190, "←", self._backspace, visible=True))
        self.add_button(Button(275, 160, 310, 190, "Enter", self._submit, visible=True))

    def _toggle_mode(self):
        # Delay rebuild to avoid overlapping immediate touch
        def delayed_rebuild():
            self.mode = "symbols" if self.mode == "letters" else "letters"
            self._build_keys()
            self.screen_manager.redraw()

        threading.Timer(0.1, delayed_rebuild).start()


    def _toggle_shift(self):
        self.shift = not self.shift
        self._build_keys()

    def _append_char(self, c):
        self.input_text += c
        print(f"[Keyboard] Input now: '{self.input_text}'")

    def _backspace(self):
        self.input_text = self.input_text[:-1]

    def _submit(self):
        self.on_submit(self.input_text)
        self.screen_manager.set_screen(self.screen_manager.previous_screen)

    def draw(self, draw_obj):
        img = Image.new("RGB", (device.width, device.height), "black")
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        try:
            fontTitle = ImageFont.truetype("pixem.otf", 18)
        except IOError:
            print("pixem.otf not found. Using default font.")
        draw.text((10, 10), f"{self.prompt}:", fill="white", font=fontTitle)
        draw.text((10, 40), self.input_text, fill="cyan", font=font)
        for btn in self.buttons:
            btn.draw(draw)
        device.display(img)

# --- Main Menu Screen ---
class MainMenuScreen(Screen):
    def __init__(self, screen_manager):
        super().__init__()
        self.screen_manager = screen_manager
        try:
            self.bg_image = Image.open("mainmenu.png").convert("RGB").resize((device.width, device.height))
        except FileNotFoundError:
            print("⚠️ mainmenu.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")

        self.add_button(Button(60, 100, 260, 130, "Menu1",
                               lambda: screen_manager.set_screen(ImageScreen("mreport.png", screen_manager))))
        self.add_button(Button(60, 140, 260, 165, "Menu2",
                               lambda: screen_manager.set_screen(ImageScreen("aconditions.png", screen_manager))))
        self.add_button(Button(60, 175, 260, 200, "Menu3",
                               lambda: screen_manager.set_screen(ImageScreen("config.png", screen_manager))))
        self.add_button(Button(60, 210, 260, 230, "Menu4",
                               lambda: screen_manager.set_screen(ImageScreen("update.png", screen_manager))))

    def draw(self, draw_obj):
        device.display(self.bg_image.copy())

# --- Select Resort Screen ---        
class SelectResortScreen(Screen):
    def __init__(self, screen_manager):
        super().__init__()
        self.screen_manager = screen_manager
        self.skiHills = ["Sun Peaks", "Silver Star", "Big White", "Whistler", "Revelstoke", "Kicking Hrse", "Lake Louise", "Banff", "Red Mountain", "White Water"]
        self.current_index = 0

        try:
            self.bg_image = Image.open("select_resort.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("⚠️ select_resort.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        # Back button (invisible)
        self.add_button(Button(270, 190, 300, 220, "Back",
                               lambda: screen_manager.set_screen(ImageScreen("config.png", screen_manager)),
                               visible=False))

        # Up and Down scroll buttons
        self.add_button(Button(272, 108, 298, 135, "Up", self.scroll_up, visible=False))
        self.add_button(Button(272, 140, 298, 165, "Down", self.scroll_down, visible=False))

        # Confirm selection by touching the current line
        self.add_button(Button(60, 175, 260, 200, "SelectCurrent", self.confirm_selection, visible=False))

    def confirm_selection(self):
        index = self.current_index
        selected = self.skiHills[index]
        try:
            with open("skihill.conf", "w") as f:
                f.write(str(index))
            print(f"[SelectResort] Selected: '{selected}' (index {index}) saved to skihill.conf")
        except Exception as e:
            print(f"[ERROR] Failed to write skihill.conf: {e}")
        
        # ✅ Return to config screen
        self.screen_manager.set_screen(ImageScreen("config.png", self.screen_manager))

    def scroll_up(self):
        if self.current_index > 0:
            self.current_index -= 1
        print(f"[SelectResort] Scrolled up to index {self.current_index}")
        
    def scroll_down(self):
        if self.current_index < len(self.skiHills) - 1:
            self.current_index += 1
        print(f"[SelectResort] Scrolled down to index {self.current_index}")

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        
        try:
            font = ImageFont.truetype("pixem.otf", 18)
        except IOError:
            print("⚠️ pixem.otf not found. Using default font.")
            font = ImageFont.load_default()

        if self.image_missing:
            font = ImageFont.load_default()
            msg = "select_resort.png not found"
            w, h = draw.textsize(msg, font=font)
            draw.text(((device.width - w) // 2, (device.height - h) // 2), msg, fill="white", font=font)

        draw.text((73, 105), "Select Resort", fill="white", font=font)
        
        # Draw previous, current, and next items
        if self.current_index > 0:
            draw.text((73, 140), self.skiHills[self.current_index - 1], fill="gray", font=font)
        draw.text((73, 175), self.skiHills[self.current_index], fill="white", font=font)
        if self.current_index < len(self.skiHills) - 1:
            draw.text((73, 207), self.skiHills[self.current_index + 1], fill="gray", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        device.display(img)
        
class ConfigWiFiScreen(Screen):
    def __init__(self, screen_manager):
        super().__init__()
        self.screen_manager = screen_manager

        # You populate this list from your own scanning logic
        self.ssid_list = get_available_ssids()  # e.g. ["MyWiFi", "GuestNet", "Starlink"]
        self.current_index = 0
        self.ssid = self.ssid_list[self.current_index] if self.ssid_list else ""
        self.password = ""

        try:
            self.bg_image = Image.open("config_wifi.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("⚠️ config_wifi.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        # SSID up/down navigation
        self.add_button(Button(272, 108, 298, 135, "SSID_UP", self.scroll_up, visible=False))
        self.add_button(Button(272, 140, 298, 165, "SSID_DOWN", self.scroll_down, visible=False))

        # Password entry via keyboard
        self.add_button(Button(60, 210, 260, 230, "PASSWORD",
                               lambda: self._open_keyboard("Enter PASSWORD", self.set_password), visible=False))

        # Back button → Save config
        self.add_button(Button(270, 190, 310, 220, "Back", self.save_and_exit, visible=False))

    def scroll_up(self):
        if self.current_index > 0:
            self.current_index -= 1
            self.ssid = self.ssid_list[self.current_index]
            print(f"[WiFi] SSID changed to: {self.ssid}")

    def scroll_down(self):
        if self.current_index < len(self.ssid_list) - 1:
            self.current_index += 1
            self.ssid = self.ssid_list[self.current_index]
            print(f"[WiFi] SSID changed to: {self.ssid}")

    def _open_keyboard(self, prompt, callback):
        self.screen_manager.previous_screen = self
        self.screen_manager.set_screen(KeyboardScreen(prompt, callback, self.screen_manager))

    def set_password(self, text):
        self.password = text
        print(f"[WiFi] PASSWORD set.")

    def save_and_exit(self):
        try:
            with open("wpa_supplicant.conf", "w") as f:
                f.write(
                    f'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n'
                    f'update_config=1\n\n'
                    f'network={{\n'
                    f'    ssid="{self.ssid}"\n'
                    f'    psk="{self.password}"\n'
                    f'    key_mgmt=WPA-PSK\n'
                    f'}}\n'
                )
            print("[WiFi] wpa_supplicant.conf saved.")

        except Exception as e:
            print(f"[ERROR] Failed to save or apply config: {e}")

        threading.Thread(target=reconfigure_wifi, daemon=True).start()         

        self.screen_manager.set_screen(ImageScreen("config.png", self.screen_manager))

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("pixem.otf", 18)
        except IOError:
            print("pixem.otf not found. Using default font.")
            font = ImageFont.load_default()
        draw.text((73, 105), "Wifi SSID", fill="white", font=font)
        if self.ssid_list:
            draw.text((73, 140), self.ssid_list[self.current_index][:14], fill="white", font=font)
        draw.text((73, 175), "PASSWORD", fill="white", font=font)
        draw.text((73, 207), f"{self.password}", fill="white", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        device.display(img)

# --- Image-based Submenu Screen ---
class ImageScreen(Screen):
    def __init__(self, image_file, screen_manager):
        super().__init__()
        self.image_file = image_file
        self.screen_manager = screen_manager
        try:
            self.bg_image = Image.open(image_file).convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print(f"⚠️ {image_file} not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        # Back button (always present, invisible)
        self.add_button(Button(270, 190, 300, 220, "Back",
                               lambda: screen_manager.set_screen(MainMenuScreen(screen_manager)),
                               visible=False))

        # Add additional buttons only for config.png
        if image_file == "config.png":
            self.add_button(Button(60, 140, 260, 165, "Select Resort",
                                   lambda: screen_manager.set_screen(SelectResortScreen(screen_manager))))
            self.add_button(Button(60, 175, 260, 200, "Config WiFi",
                                   lambda: screen_manager.set_screen(ConfigWiFiScreen(screen_manager))))
            self.add_button(Button(60, 210, 260, 230, "Misc.",
                                   lambda: screen_manager.set_screen(ImageScreen("misc.png", screen_manager))))


    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)

        if self.image_file == "config.png":
            try:
                font = ImageFont.truetype("pixem.otf", 18)
            except IOError:
                print("pixem.otf not found. Using default font.")
                font = ImageFont.load_default()
            draw.text((73, 105), "Configuration", fill="white", font=font)
            draw.text((73, 140), "Select Resort", fill="white", font=font)    
            draw.text((73, 175), "Config Wifi", fill="white", font=font)
            draw.text((73, 207), "Misc. Settings", fill="white", font=font)
            
        if self.image_missing:
            font = ImageFont.load_default()
            msg = f"{os.path.basename(self.image_file)} not found"
            w, h = draw.textsize(msg, font=font)
            draw.text(((device.width - w) // 2, (device.height - h) // 2),
                      msg, fill="white", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        device.display(img)

# --- Screen Manager ---
class ScreenManager:
    def __init__(self):
        self.current = None

    def set_screen(self, screen):
        self.current = screen
        self.redraw()

    def draw(self, draw_obj):
        if self.current:
            self.current.draw(draw_obj)

    def handle_touch(self, x, y):
        if self.current:
            self.current.handle_touch(x, y)
            self.redraw()

    def redraw(self):
        img = Image.new("RGB", (device.width, device.height), "black")
        draw = ImageDraw.Draw(img)
        self.draw(draw)

# --- Main Loop ---
def main():
    # Intialize touchscreen
    touch = XPT2046()
    calibrator = TouchCalibrator()
    
    # Start heartbeat to detect hangups
    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    # Display Splash Screen
    splash = Image.open("splashlogo.png").convert("RGB")
    splash = splash.resize((device.width, device.height))
    device.display(splash)
    time.sleep(5)
    
    try:
        if os.path.exists(CALIBRATION_FILE):
#            ans = input("Use saved calibration? [Y/n]: ").strip().lower()
#            if ans != 'n':
                calibrator.load()
#            else:
#                calibrator.calibrate(touch)
        else:
            calibrator.calibrate(touch)

        screen_manager = ScreenManager()
        screen_manager.set_screen(MainMenuScreen(screen_manager))

        while True:
            coord = touch.read_touch()
            if coord:
                mapped = calibrator.map_raw_to_screen(*coord)
                print(f"Touch @ {mapped}")
                screen_manager.handle_touch(*mapped)
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("Exiting.")
    finally:
        touch.close()

if __name__ == "__main__":
    main()

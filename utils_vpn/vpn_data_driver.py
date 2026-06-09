import undetected_chromedriver as uc

def connect_driver(headless=False):
    options = uc.ChromeOptions()

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

    # 🔻 Smaller viewport = less layout + font data
    options.add_argument("--window-size=1366,768")

    # options.add_argument("--incognito")
    # options.add_argument("--disable-notifications")

    # 🔻 Disable background & sync services
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-features=Translate,BackForwardCache")

    # 🔻 Disable Chrome telemetry & pings
    options.add_argument("--metrics-recording-only")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--disable-component-update")

    # 🔻 Prevent prefetch & speculative loads
    options.add_argument("--disable-features=NetworkService,PrefetchPrivacyChanges")

    # 🔻 Preferences: BLOCK heavy content
    prefs = {
        "profile.default_content_setting_values": {
            # "images": 2,        # already good
            "plugins": 2,
            "geolocation": 2,
            "notifications": 2,
            "media_stream": 2, # 🔻 block camera/audio
            "fonts": 2,        # 🔥 BIG data saver
            "popups": 2,
        },
        "profile.managed_default_content_settings": {
            # "images": 2,
            "fonts": 2,
        },
        "profile.password_manager_enabled": False,
        "credentials_enable_service": False,
        "intl.accept_languages": "en-US,en"
    }

    options.add_experimental_option("prefs", prefs)

    try:
        # version_main must match your installed Chrome (chrome://version). E.g. 145 or 146.
        driver = uc.Chrome(options=options, version_main=148)
        # Avoid Unicode emojis: some Windows consoles use cp1252 and crash on ✅/❌
        print("[OK] Low-data Undetected Chrome Driver started")
        return driver
    except Exception as e:
        print(f"[ERROR] Error connecting to driver: {e}")
        return None

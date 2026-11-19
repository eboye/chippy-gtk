import warnings

VERSION = "0.1.6"
APP_ID = "io.github.eboye.Chippy"

# python-miio 0.5.12 trips a harmless FutureWarning on Python 3.13+ every time it's imported.
warnings.filterwarnings("ignore", category=FutureWarning, module=r"miio\.")

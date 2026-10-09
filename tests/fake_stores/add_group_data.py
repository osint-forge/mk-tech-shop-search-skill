"""Append the fixtures for mkshop's group / match tests (2026-10-03) to the fake
catalogues: a Setec bare-code Sony headphone with its marketed-name listings,
JBL Live 770NC in each shop's title style (spaced code, WHT, LIVE770NCBLK,
Sand). Idempotent: records with ids starting 'G' are replaced. The committed data/ already
holds them; rerun this by hand (python3 tests/fake_stores/add_group_data.py) after editing it."""
import json, os
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = {"setec": "https://setec.mk/products/", "anhoch": "https://www.anhoch.com/products/",
        "ddstore": "https://ddstore.mk/mk/", "neptun": "https://www.neptun.mk/categories/",
        "zirafamall": "https://zirafamall.mk/", "ananas": "https://ananas.mk/proizvod/",
        "tehnomarket": "https://tehnomarket.com.mk/product/"}
HP = "Аудио > Слушалки"


def rec(store, rid, title, price, slug, brand, ean=None, cat=HP, **kw):
    r = {"store": store, "id": rid, "sku": kw.pop("sku", rid), "title": title, "url": BASE[store] + slug,
         "brand": brand, "price_mkd": price, "regular_price_mkd": kw.pop("regular", None),
         "in_stock": kw.pop("in_stock", True), "stock_note": "на залиха", "category": cat, "ean": ean,
         "mpn": None, "warranty": "24 месеци", "specs": kw.pop("specs", title + "."),
         "per_location_stock": None, "extra": None}
    r.update(kw)
    return r


ULT = "4548736156432"
NEW = {
    "setec": [rec("setec", "G101", "SONY WHULT900NB.CE7", 8929, "sony-whult900nbce7-G101", "SONY", ULT,
                  "Аудио > Bluetooth слушалки", regular=11390, attributes={"Боја": "Црна"},
                  specs="SONY WHULT900NB.CE7, Wireless BLUETOOTH Noise Canceling Headphones, Up to 30 hours "
                        "of playback time, Black"),
              rec("setec", "G102", "SONY WHULT900NH.CE7", 8929, "sony-whult900nhce7-G102", "SONY", "4548736156449",
                  "Аудио > Bluetooth слушалки", attributes={"Боја": "Сива"}),
              rec("setec", "G108", "JBL T770NC Wireless Over-Ear Headphones Black", 4999,
                  "jbl-t770nc-wireless-over-ear-headphones-black-G108", "JBL", "6925281974571")],
    "zirafamall": [rec("zirafamall", "G103", "Слушалки Sony ULT Wear WHULT900N, безжични, со длабок бас, црни",
                       10690, "slushalki-sony-ult-wear-G103", None, ULT, sku="15000001mo",
                       seller="Basics from GjirafaMall")],
    "anhoch": [rec("anhoch", "G104", "Headphones Sony ULT WEAR Bluetooth Wireless Noise Cancelling Black", 9490,
                   "headphones-sony-ult-wear-black-G104", "Sony"),
               rec("anhoch", "G105", "Headphones Sony ULT WEAR Bluetooth Wireless Noise Cancelling Forest Gray",
                   9490, "headphones-sony-ult-wear-forest-gray-G105", "Sony"),
               rec("anhoch", "G106", "Headphones Sony ULT WEAR Bluetooth Wireless Noise Cancelling White", 9490,
                   "headphones-sony-ult-wear-white-G106", "Sony"),
               rec("anhoch", "G201", "Headphones JBL Live 770NC ANC Wireless Black", 10980,
                   "headphones-jbl-live-770nc-anc-wireless-black-G201", "JBL"),
               rec("anhoch", "G202", "Headphones JBL Live 770NC ANC Wireless Sand", 10980,
                   "headphones-jbl-live-770nc-anc-wireless-sand-G202", "JBL"),
               rec("anhoch", "G203", "Headphones JBL Live 770NC ANC Wireless Blue", 10980,
                   "headphones-jbl-live-770nc-anc-wireless-blue-G203", "JBL")],
    "ddstore": [rec("ddstore", "G107", "SONY SRSULT10B.CE7 Wireless Speaker Black", 4849,
                    "sony-srsult10b-ce7-wireless-speaker-black-G107", "SONY", "4548736160262", "Аудио > Звучници",
                    in_stock=None)],
    "tehnomarket": [rec("tehnomarket", "G204", "JBL LIVE 770 NC BLUE СЛУШАЛКИ", 9980, "jbl-live-770-nc-blue-G204",
                        "JBL", regular=12999),
                    rec("tehnomarket", "G205", "JBL LIVE 770 NC BLACK СЛУШАЛКИ", 9980, "jbl-live-770-nc-black-G205",
                        "JBL", regular=12999)],
    "neptun": [rec("neptun", "G206", "BT слушалки JBL LIVE 770 NC WHT", 10999, "JBL-LIVE-770-NC-WHT-G206", "JBL")],
    "ananas": [rec("ananas", "G207", "JBL Безжични слушалки LIVE770NCBLK", 6990, "jbl-bezzicni-slusalki-live770ncblk/G207",
                   "JBL", seller="Аудио Центар Битола", regular=8990)],
}
# 2026-10-04: dotted Setec codes, 'w/Microphone' titles, laptop configurations (real titles).
XM5L, XM5S = "4548736134294", "4548736132597"
SPECS_L = ("SONY WH1000XM5L.CE7 ( Midnight Blue ), Overhead Wireless Noise Cancelling Headphones, 4 Hz - 40.000 Hz, "
           "Wireless freedom with BLUETOOTH, Speak-to-chat")
LAP = "Компјутери > Лаптопи"
BASE["gjirafa50"] = "https://gjirafa50.mk/"
NEW["setec"] += [rec("setec", "G301", "SONY WH1000XM5L.CE7 ( Midnight Blue )", 19599, "sony-wh1000xm5lce7-midnight-blue-G301",
                     "SONY", XM5L, "Аудио > Bluetooth слушалки", attributes={"Боја": "Сина"}, specs=SPECS_L),
                 rec("setec", "G304", "SONY WH1000XM5S.CE7 ( Platinum Silver )", 19599,
                     "sony-wh1000xm5sce7-platinum-silver-G304", "SONY", XM5S, "Аудио > Bluetooth слушалки",
                     attributes={"Боја": "Беж"}, specs=SPECS_L.replace("WH1000XM5L", "WH1000XM5S")
                     .replace("Midnight Blue", "Platinum Silver"))]
# Anhoch's own search finds 'WH1000XM5L' but not 'WH1000XM5L.CE7'
NEW["anhoch"] += [rec("anhoch", "G302", "Headphones Sony WH1000XM5L Noise Cancelling Bluetooth w/Microphone Blue", 17980,
                      "headphones-sony-wh1000xm5l-noise-cancelling-bluetooth-wmicrophone-blue-G302", "Sony"),
                  rec("anhoch", "G303", "Headphones Sony WH1000XM5P Noise Cancelling Bluetooth w/Microphone Smoky Pink",
                      17980, "headphones-sony-wh1000xm5p-noise-cancelling-bluetooth-wmicrophone-smoky-pink-G303", "Sony")]
NEW["gjirafa50"] = [rec("gjirafa50", "G401", 'Laptop за гејминг ASUS TUF A15 FA507NU, 15.6", Ryzen 5, 16GB, 512GB, RTX 4050, црн',
                        56090, "laptop-asus-tuf-a15-fa507nu-ryzen-5-G401", "ASUS", None, LAP, sku="14133777mo"),
                    rec("gjirafa50", "G402", 'Laptop за гејминг ASUS TUF Gaming A15 FA507NU, 15.6", Ryzen 7, RTX 4050, црн',
                        69890, "laptop-asus-tuf-gaming-a15-fa507nu-ryzen-7-G402", "ASUS", None, LAP, sku="MOBASUNOTBAJEa")]
for store, rows in NEW.items():
    path = os.path.join(HERE, "data", store + ".json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data = [r for r in data if not str(r.get("id", "")).startswith("G")] + rows
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
print("fixtures:", {k: len(v) for k, v in NEW.items()})

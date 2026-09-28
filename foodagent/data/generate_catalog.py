"""Generate catalog_extra.json: the design doc's "30–50 restaurants with 20–40 items each".

    python -m foodagent.data.generate_catalog      # rewrites data/catalog_extra.json (deterministic)
    python -m foodagent.db init --reset            # then reload the database

Menus are drawn from per-cuisine dish libraries below. Declared allergens come from each dish's
ingredients (ingredient_allergens.json) plus explicit may-contain notes, and the file includes the
doc's deliberate traps so the safety filters are exercised:
cashew gravies, peanut salans and chutneys, egg in "veg"-looking items, shared-fryer kitchens,
unverified allergen data, out-of-stock dishes, a low-rated outlet, a far one, one closed at dinner,
and a few dishes whose restaurant under-declared an allergen (the database's derived rows catch them).
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "catalog_extra.json"
SEED = 42
MEAT = {"chicken", "mutton", "fish", "prawn"}
ANIMAL = {"dairy", "egg"}

# name | course | diet | serves | price | ingredients | primary | spice | tastes | tags | famous_in | may_contain
POOLS = {
    "north_indian": """
Paneer Butter Masala|main|veg|2|300|paneer,tomato,butter,cream,cashew|paneer|1|rich:4,sweet:2|curry,shareable|delhi|
Kadai Paneer|main|veg|2|290|paneer,capsicum,onion,tomato,spices|paneer|3|tangy:2,smoky:1|curry,shareable|delhi,punjab|
Palak Paneer|main|veg|2|280|paneer,spinach,cream,garlic|paneer|2|savoury:3|curry,shareable|punjab|
Shahi Paneer|main|veg|2|310|paneer,cashew,cream,khoya|paneer|1|rich:4,sweet:2|curry,shareable|delhi|
Malai Kofta|main|veg|2|300|paneer,potato,cashew,cream|paneer|1|rich:4,sweet:3|curry,shareable||
Dal Makhani|main|veg|2|250|black lentil,butter,cream|black lentil|1|rich:4|dal,shareable|punjab|
Dal Tadka|main|vegan|2|200|toor dal,cumin,garlic,tomato|toor dal|2|savoury:3|dal,shareable||
Chana Masala|main|vegan|2|220|chickpea,onion,tomato,spices|chickpea|3|tangy:3|curry,shareable|punjab,delhi|
Rajma Masala|main|vegan|2|220|kidney bean,onion,tomato|kidney bean|2|savoury:3|curry,shareable|punjab|
Aloo Gobi|main|vegan|2|210|potato,cauliflower,turmeric|potato|2|savoury:3|shareable||
Mix Veg Curry|main|veg|2|230|vegetables,cream,spices|vegetables|2|savoury:2|curry,shareable||
Bhindi Masala|main|vegan|2|220|okra,onion,spices|okra|2|savoury:3|shareable||
Butter Chicken|main|non_veg|2|340|chicken,butter,cream,cashew,tomato|chicken|1|rich:4,sweet:2|curry,shareable|delhi|
Chicken Curry|main|non_veg|2|320|chicken,onion,tomato,spices|chicken|3|savoury:3|curry,shareable||
Kadai Chicken|main|non_veg|2|330|chicken,capsicum,tomato|chicken|3|tangy:2|curry,shareable|punjab|
Mutton Rogan Josh|main|non_veg|2|420|mutton,curd,spices|mutton|3|rich:3|curry,shareable||
Egg Curry|main|egg|2|220|egg,onion,tomato|egg|3|savoury:3|curry,shareable||
Jeera Rice|rice|vegan|2|170|basmati rice,cumin|rice|0|savoury:2|shareable||
Steamed Rice|rice|vegan|2|140|rice|rice|0||shareable||
Veg Pulao|rice|veg|2|200|basmati rice,vegetables,ghee|rice|1|savoury:2|shareable||
Butter Naan|bread|veg|1|55|flour,butter|flour|0|rich:1|||
Garlic Naan|bread|veg|1|65|flour,butter,garlic|flour|0|savoury:2|||
Tandoori Roti|bread|vegan|1|30|wheat flour|wheat flour|0||||
Lachha Paratha|bread|veg|1|60|wheat flour,ghee|wheat flour|0|rich:1|||
Missi Roti|bread|vegan|1|45|gram flour,wheat flour|gram flour|1|savoury:2|||
Boondi Raita|side|veg|2|90|curd,boondi|curd|0|sour:1|shareable||
Green Salad|side|vegan|2|80|cucumber,onion,tomato|cucumber|0||shareable||
Paneer Tikka|starter|veg|2|280|paneer,curd,capsicum,spices|paneer|3|smoky:3|shareable|delhi|tree_nut
Hara Bhara Kebab|starter|vegan|2|240|spinach,potato,peas|spinach|2|savoury:3|shareable||
Chicken Tikka|starter|non_veg|2|320|chicken,curd,spices|chicken|3|smoky:3|shareable|punjab|
Gulab Jamun (2 pcs)|dessert|veg|2|110|khoya,sugar,flour|khoya|0|sweet:4|||tree_nut
Rasmalai (2 pcs)|dessert|veg|2|140|milk,sugar,pistachio|milk|0|sweet:4|||
Gajar Halwa|dessert|veg|2|130|carrot,milk,ghee,cashew|carrot|0|sweet:4|||
Sweet Lassi|beverage|veg|1|90|curd,sugar|curd|0|sweet:3|||
Masala Chaas|beverage|veg|1|60|curd,cumin|curd|1|sour:2|||
""",
    "mughlai": """
Chicken Korma|main|non_veg|2|360|chicken,cashew,cream,curd|chicken|2|rich:4|curry,shareable||
Mutton Korma|main|non_veg|2|440|mutton,cashew,cream,curd|mutton|2|rich:4|curry,shareable||
Navratan Korma|main|veg|2|300|vegetables,cashew,cream,raisin|vegetables|1|rich:4,sweet:3|curry,shareable||
Paneer Pasanda|main|veg|2|330|paneer,cashew,cream|paneer|1|rich:4|curry,shareable||
Paneer Lababdar|main|veg|2|320|paneer,tomato,cream,cashew|paneer|2|rich:3|curry,shareable|delhi|
Nihari|main|non_veg|2|460|mutton,wheat flour,spices|mutton|3|rich:4|curry,shareable|delhi|
Dal Bukhara|main|veg|2|280|black lentil,butter,cream|black lentil|1|rich:4|dal,shareable|delhi|
Mushroom Do Pyaza|main|vegan|2|260|mushroom,onion,tomato|mushroom|2|savoury:3|curry,shareable||
Baingan Bharta|main|vegan|2|230|brinjal,onion,tomato|brinjal|2|smoky:3|shareable|punjab|
Pindi Chole|main|vegan|2|240|chickpea,onion,spices|chickpea|3|tangy:3|curry,shareable|delhi|
Mutton Rogan Josh|main|non_veg|2|440|mutton,curd,spices|mutton|3|rich:3|curry,shareable||
Seekh Kebab|starter|non_veg|2|340|mutton,onion,spices|mutton|3|smoky:3|shareable|delhi|
Galouti Kebab|starter|non_veg|2|380|mutton,spices,ghee|mutton|2|rich:4|shareable||
Veg Seekh Kebab|starter|vegan|2|260|vegetables,potato,spices|vegetables|2|smoky:2|shareable||
Tandoori Chicken (half)|starter|non_veg|2|320|chicken,curd,spices|chicken|3|smoky:4|shareable|punjab|
Jeera Rice|rice|vegan|2|180|basmati rice,cumin|rice|0|savoury:2|shareable||
Sheermal|bread|veg|1|80|flour,milk,ghee,saffron|flour|0|sweet:2|||
Roomali Roti|bread|veg|1|45|flour,milk|flour|0||||
Butter Naan|bread|veg|1|60|flour,butter|flour|0|rich:1|||
Tandoori Roti|bread|vegan|1|35|wheat flour|wheat flour|0||||
Burani Raita|side|veg|2|90|curd,garlic|curd|0|sour:1|shareable|hyderabad|
Shahi Tukda|dessert|veg|2|160|bread,milk,ghee,almond|bread|0|sweet:4,rich:3|||
Phirni|dessert|veg|2|120|rice,milk,pistachio|milk|0|sweet:3|||
Kulfi|dessert|veg|1|90|milk,pistachio|milk|0|sweet:3|||
""",
    "biryani": """
Chicken Dum Biryani|meal|non_veg|2|340|basmati rice,chicken,curd,fried onion,spices|chicken,rice|3|savoury:4|shareable|hyderabad|
Mutton Dum Biryani|meal|non_veg|2|420|basmati rice,mutton,curd,fried onion,spices|mutton,rice|3|savoury:4|shareable|hyderabad|
Veg Dum Biryani|meal|veg|2|260|basmati rice,vegetables,curd,fried onion|rice,vegetables|2|savoury:3|shareable|hyderabad|
Paneer Biryani|meal|veg|2|290|basmati rice,paneer,curd,fried onion|paneer,rice|2|savoury:3|shareable||
Egg Biryani|meal|egg|2|260|basmati rice,egg,fried onion,spices|egg,rice|3|savoury:3|shareable||
Mushroom Biryani|meal|vegan|2|270|basmati rice,mushroom,fried onion|mushroom,rice|2|savoury:3|shareable||
Family Veg Biryani|meal|veg|4|520|basmati rice,vegetables,curd,fried onion,cashew|rice,vegetables|2|savoury:3|shareable||
Mirchi ka Salan|side|vegan|2|120|green chilli,peanut,sesame,tamarind|green chilli|3|tangy:3|shareable|hyderabad|
Bagara Baingan|side|vegan|2|160|brinjal,peanut,sesame,tamarind|brinjal|2|tangy:3|shareable|hyderabad|
Burani Raita|side|veg|2|80|curd,garlic|curd|0|sour:1|shareable||
Onion Raita|side|veg|2|70|curd,onion|curd|0|sour:1|shareable||
Chicken 65|starter|non_veg|2|280|chicken,curd,curry leaves,cornflour|chicken|4|savoury:3|shareable||
Paneer 65|starter|veg|2|260|paneer,curd,curry leaves,cornflour|paneer|4|savoury:3|shareable||
Apollo Fish|starter|non_veg|2|340|fish,curd,spices|fish|3|savoury:3|shareable|hyderabad|
Haleem|main|non_veg|2|360|mutton,broken wheat,toor dal,ghee|mutton|2|rich:4|shareable|hyderabad|
Dalcha|main|vegan|2|180|toor dal,tamarind,bottle gourd|toor dal|2|tangy:2|dal,shareable|hyderabad|
Chicken Korma|main|non_veg|2|320|chicken,curd,cashew|chicken|2|rich:3|curry,shareable||
Kadai Veg|main|veg|2|240|vegetables,capsicum,tomato,butter|vegetables|3|savoury:3|curry,shareable||
Dal Fry|main|vegan|2|190|toor dal,garlic,cumin|toor dal|2|savoury:3|dal,shareable||
Double ka Meetha|dessert|veg|2|140|bread,milk,ghee,cashew|bread|0|sweet:4|||
Qubani ka Meetha|dessert|veg|2|150|apricot,sugar,cream,almond|apricot|0|sweet:4|||
Irani Chai|beverage|veg|1|40|milk,tea|tea|0|sweet:2|||
""",
    "south_indian": """
Masala Dosa|meal|veg|1|110|rice batter,potato,ghee|rice batter|1|savoury:3||karnataka|
Plain Dosa|meal|vegan|1|80|rice batter|rice batter|0|savoury:2||karnataka|
Rava Dosa|meal|veg|1|120|semolina,rice flour,ghee|semolina|1|savoury:3||karnataka|
Mysore Masala Dosa|meal|veg|1|130|rice batter,potato,red chutney,ghee|rice batter|3|savoury:3||karnataka|
Idli (2 pcs)|meal|vegan|1|60|rice batter,urad dal|rice batter|0|savoury:1|||
Ven Pongal|meal|veg|1|120|rice,moong dal,ghee,cashew|rice|1|savoury:3,rich:2||tamil nadu|
Bisi Bele Bath|meal|veg|1|130|rice,toor dal,vegetables,ghee|rice|2|tangy:2||karnataka|
South Indian Thali|meal|veg|1|220|rice,sambar,rasam,poriyal,curd|rice|2|savoury:3|complete_meal||
Curd Rice|rice|veg|1|100|rice,curd|rice|0|sour:2|||
Lemon Rice|rice|vegan|1|100|rice,peanut,curry leaves|rice|1|tangy:3|||
Puliyogare|rice|vegan|1|110|rice,tamarind,peanut|rice|2|tangy:4||karnataka|
Sambar|main|vegan|2|120|toor dal,vegetables,tamarind|toor dal|2|tangy:3|dal,shareable||
Rasam|main|vegan|2|90|tamarind,tomato,pepper|tamarind|2|tangy:4,sour:3|shareable||
Avial|main|veg|2|180|vegetables,coconut,curd|vegetables|1|savoury:2|shareable|kerala|
Vegetable Kurma|main|veg|2|190|vegetables,coconut,cashew|vegetables|2|rich:3|curry,shareable||
Chicken Chettinad|main|non_veg|2|320|chicken,coconut,pepper,spices|chicken|4|savoury:4|curry,shareable|tamil nadu|
Kerala Fish Curry|main|non_veg|2|340|fish,coconut,tamarind|fish|3|tangy:3|curry,shareable|kerala|
Prawn Moilee|main|non_veg|2|420|prawn,coconut,curry leaves|prawn|2|rich:3|curry,shareable|kerala|
Malabar Parotta|bread|egg|1|55|flour,egg,ghee|flour|0|rich:2||kerala|
Appam|bread|vegan|1|50|rice batter,coconut|rice batter|0|sweet:1||kerala|
Medu Vada (2 pcs)|starter|vegan|1|70|urad dal,curry leaves|urad dal|1|savoury:3||karnataka|
Coconut Chutney|side|vegan|2|40|coconut,green chilli|coconut|1|savoury:2|shareable||
Peanut Chutney|side|vegan|2|45|peanut,green chilli|peanut|1|savoury:2|shareable||
Kesari Bath|dessert|veg|1|70|semolina,ghee,sugar,cashew|semolina|0|sweet:4||karnataka|
Mysore Pak|dessert|veg|1|80|gram flour,ghee,sugar|gram flour|0|sweet:4,rich:3||karnataka|
Payasam|dessert|veg|1|90|milk,rice,sugar,cashew|milk|0|sweet:4||kerala|
Filter Coffee|beverage|veg|1|45|milk,coffee|coffee|0|bitter:2||tamil nadu|
Neer Mor|beverage|veg|1|40|curd,curry leaves|curd|1|sour:2|||
""",
    "indo_chinese": """
Veg Manchurian (gravy)|main|vegan|2|220|cabbage,cornflour,soy sauce,garlic|cabbage|3|savoury:3|shareable|kolkata|
Chilli Paneer (gravy)|main|veg|2|260|paneer,capsicum,soy sauce,chilli|paneer|4|tangy:2,savoury:3|shareable|kolkata|
Gobi Manchurian|main|vegan|2|220|cauliflower,cornflour,soy sauce|cauliflower|3|savoury:3|shareable||
Hot Garlic Veg|main|vegan|2|230|vegetables,garlic,soy sauce|vegetables|3|savoury:3|shareable||
Mapo Tofu|main|vegan|2|260|tofu,chilli,soy sauce|tofu|4|savoury:3|shareable||
Kung Pao Paneer|main|veg|2|270|paneer,peanut,dry chilli,soy sauce|paneer|3|savoury:3,sweet:1|shareable||
Chilli Chicken|main|non_veg|2|290|chicken,soy sauce,capsicum,egg|chicken|4|savoury:3|shareable|kolkata|
Chicken Manchurian|main|non_veg|2|280|chicken,soy sauce,egg,cornflour|chicken|3|savoury:3|shareable||
Kung Pao Chicken|main|non_veg|2|300|chicken,peanut,dry chilli,soy sauce|chicken|3|savoury:3,sweet:1|shareable||
Chilli Fish|main|non_veg|2|320|fish,soy sauce,capsicum|fish|3|savoury:3|shareable||
Schezwan Prawns|main|non_veg|2|380|prawn,dry chilli,soy sauce|prawn|4|savoury:3|shareable||
Veg Hakka Noodles|noodles|vegan|2|200|noodles,cabbage,soy sauce|noodles|1|savoury:3|shareable||
Schezwan Noodles|noodles|vegan|2|220|noodles,dry chilli,soy sauce|noodles|4|savoury:3|shareable||
Chicken Hakka Noodles|noodles|non_veg|2|250|noodles,chicken,egg,soy sauce|noodles,chicken|1|savoury:3|shareable||
Veg Fried Rice|rice|vegan|2|200|rice,vegetables,soy sauce|rice|1|savoury:3|shareable||
Egg Fried Rice|rice|egg|2|220|rice,egg,soy sauce|rice|1|savoury:3|shareable||
Chicken Fried Rice|rice|non_veg|2|250|rice,chicken,egg,soy sauce|rice,chicken|1|savoury:3|shareable||
Burnt Garlic Rice|rice|vegan|2|210|rice,garlic|rice|1|savoury:3|shareable||
Veg Spring Roll|starter|vegan|2|180|flour,cabbage,carrot|cabbage|1|savoury:2|shareable||
Honey Chilli Potato|starter|veg|2|200|potato,honey,sesame|potato|2|sweet:3|shareable||
Crispy Corn|starter|vegan|2|190|corn,cornflour|corn|2|savoury:3|shareable||
Chicken Lollipop|starter|non_veg|2|280|chicken,egg,cornflour|chicken|3|savoury:3|shareable||
Veg Momos (6 pcs)|starter|vegan|1|120|flour,cabbage|cabbage|1|savoury:2|||
Chicken Momos (6 pcs)|starter|non_veg|1|150|flour,chicken|chicken|1|savoury:2|||
Sweet Corn Soup|side|vegan|2|140|corn,cornflour|corn|0|sweet:1|shareable||
Hot and Sour Soup|side|vegan|2|150|vegetables,soy sauce,vinegar|vegetables|3|sour:3|shareable||
Darsaan with Ice Cream|dessert|veg|2|160|flour,honey,sesame,milk|flour|0|sweet:4|||
Date Pancake|dessert|egg|2|170|flour,dates,egg|dates|0|sweet:4|||
""",
    "street_food": """
Pav Bhaji|meal|veg|1|160|potato,vegetables,butter,bun|potato|2|savoury:3,rich:2||mumbai|
Chole Bhature|meal|veg|1|180|chickpea,flour,curd|chickpea|3|tangy:3||delhi|
Rajma Chawal|meal|vegan|1|170|kidney bean,rice|kidney bean|2|savoury:3||delhi,punjab|
Aloo Paratha (with curd)|meal|veg|1|120|wheat flour,potato,butter,curd|potato|2|savoury:3||punjab|
Paneer Paratha|meal|veg|1|150|wheat flour,paneer,butter|paneer|2|savoury:3||punjab|
Chole Kulche|meal|veg|1|150|chickpea,flour,butter|chickpea|3|tangy:3||delhi|
Misal Pav|meal|vegan|1|140|moth beans,gram flour,bun|moth beans|4|tangy:3,savoury:3||mumbai|peanut
Paneer Kathi Roll|meal|egg|1|160|flour,paneer,egg,onion|paneer|2|savoury:3||kolkata|
Chicken Kathi Roll|meal|non_veg|1|180|flour,chicken,egg,onion|chicken|2|savoury:3||kolkata|
Veg Frankie|meal|vegan|1|120|flour,potato,vegetables|potato|2|savoury:3||mumbai|
Vada Pav|starter|vegan|1|40|potato,gram flour,bun|potato|2|savoury:3||mumbai|
Aloo Tikki Chaat|starter|veg|1|90|potato,curd,tamarind chutney|potato|2|tangy:3,sweet:2||delhi|
Papdi Chaat|starter|veg|1|90|flour,curd,chickpea,tamarind chutney|chickpea|2|tangy:3,sweet:2||delhi|
Pani Puri (6 pcs)|starter|vegan|1|60|semolina,potato,tamarind|potato|3|tangy:4||mumbai|
Bhel Puri|starter|vegan|1|70|puffed rice,peanut,onion,tamarind chutney|puffed rice|2|tangy:3,sweet:2||mumbai|
Sev Puri|starter|vegan|1|80|flour,gram flour,potato|potato|2|tangy:3||mumbai|
Dahi Puri|starter|veg|1|90|semolina,curd,potato|potato|1|sweet:2,tangy:2||mumbai|
Samosa (2 pcs)|starter|vegan|1|50|flour,potato,peas|potato|2|savoury:3||delhi|
Kachori (2 pcs)|starter|vegan|1|60|flour,moong dal|moong dal|2|savoury:3||delhi|
Dabeli|starter|veg|1|60|bun,potato,peanut,pomegranate,butter|potato|2|sweet:2,tangy:2||gujarat|
Jalebi (200 g)|dessert|veg|2|90|jalebi,sugar,ghee|jalebi|0|sweet:4||delhi|
Rabri Jalebi|dessert|veg|2|140|rabri,jalebi|jalebi|0|sweet:4||delhi|tree_nut
Kulfi Falooda|dessert|veg|1|130|milk,pistachio,vermicelli|milk|0|sweet:4||delhi|
Masala Chai|beverage|veg|1|30|milk,tea|tea|1|sweet:2|||
Mango Lassi|beverage|veg|1|90|curd,mango,sugar|mango|0|sweet:4|||
""",
    "bengali": """
Kosha Mangsho|main|non_veg|2|420|mutton,onion,mustard oil|mutton|3|rich:4|curry,shareable|kolkata|
Shorshe Ilish|main|non_veg|2|480|fish,mustard,mustard oil|fish|3|savoury:4|curry,shareable|kolkata|
Chingri Malai Curry|main|non_veg|2|460|prawn,coconut,ghee|prawn|1|rich:4,sweet:2|curry,shareable|kolkata|
Macher Jhol|main|non_veg|2|320|fish,potato,tomato|fish|2|savoury:3|curry,shareable|kolkata|
Chicken Kasha|main|non_veg|2|340|chicken,onion,mustard oil|chicken|3|rich:3|curry,shareable||
Aloo Posto|main|vegan|2|200|potato,poppy seed|potato|1|savoury:3|shareable|kolkata|
Shukto|main|veg|2|210|vegetables,milk,mustard|vegetables|0|bitter:2|shareable|kolkata|
Cholar Dal|main|veg|2|180|chana dal,coconut,ghee,raisin|chana dal|1|sweet:2|dal,shareable|kolkata|
Dhokar Dalna|main|veg|2|230|chana dal,potato,ghee|chana dal|2|savoury:3|curry,shareable||
Chanar Dalna|main|veg|2|260|paneer,potato,ghee|paneer|2|rich:3|curry,shareable||
Begun Bhaja|side|vegan|2|90|brinjal,mustard oil|brinjal|1|savoury:2|shareable||
Luchi (4 pcs)|bread|veg|2|70|flour,ghee|flour|0||||
Steamed Rice|rice|vegan|2|120|rice|rice|0||shareable||
Basanti Pulao|rice|veg|2|220|rice,ghee,cashew,raisin|rice|0|sweet:3|shareable|kolkata|
Kolkata Chicken Biryani|meal|non_veg|2|360|basmati rice,chicken,potato,egg|chicken,rice|2|savoury:3|shareable|kolkata|
Fish Fry|starter|non_veg|2|280|fish,breadcrumbs,egg|fish|1|savoury:3|shareable|kolkata|
Veg Chop (2 pcs)|starter|vegan|1|80|beetroot,potato,breadcrumbs|beetroot|1|sweet:2||kolkata|
Mishti Doi|dessert|veg|1|70|milk,sugar|milk|0|sweet:4||kolkata|
Rosogolla (2 pcs)|dessert|veg|1|60|milk,sugar|milk|0|sweet:4||kolkata|
Sandesh (2 pcs)|dessert|veg|1|80|milk,sugar,pistachio|milk|0|sweet:4||kolkata|
Aam Pora Shorbot|beverage|vegan|1|70|raw mango,sugar|raw mango|0|sour:3,sweet:2|||
""",
    "gujarati": """
Gujarati Thali|meal|veg|1|280|rice,toor dal,vegetables,wheat flour,ghee,curd|rice|1|sweet:2|complete_meal|gujarat|
Dal Baati Churma|meal|veg|1|260|wheat flour,ghee,toor dal,sugar|wheat flour|1|rich:4||rajasthan|
Sabudana Khichdi|meal|vegan|1|140|sago,peanut,potato|sago|1|savoury:2||gujarat|
Masala Khichdi|meal|veg|1|160|rice,moong dal,ghee|rice|1|savoury:2|||
Undhiyu|main|vegan|2|280|vegetables,peanut,coconut,sesame|vegetables|2|sweet:2,savoury:3|shareable|gujarat|
Gatte ki Sabzi|main|veg|2|240|gram flour,curd,spices|gram flour|3|tangy:2|curry,shareable|rajasthan|
Ker Sangri|main|vegan|2|260|ker berries,sangri beans,spices|sangri beans|3|tangy:3|shareable|rajasthan|
Sev Tamatar|main|vegan|2|200|tomato,gram flour|tomato|2|tangy:3,sweet:2|curry,shareable|gujarat|
Gujarati Kadhi|main|veg|2|160|curd,gram flour|curd|1|sweet:2,sour:2|shareable|gujarat|
Dal Dhokli|main|vegan|2|200|toor dal,wheat flour,peanut|toor dal|1|sweet:2,tangy:2|dal,shareable|gujarat|
Lasaniya Bateta|main|vegan|2|210|potato,garlic,spices|potato|3|savoury:3|shareable|gujarat|
Rotli (2 pcs)|bread|veg|1|40|wheat flour,ghee|wheat flour|0||||
Bajra Rotla|bread|vegan|1|45|bajra flour|bajra flour|0||||
Methi Thepla (3 pcs)|bread|veg|1|80|wheat flour,fenugreek,curd|wheat flour|1|savoury:2||gujarat|
Jeera Rice|rice|vegan|2|150|basmati rice,cumin|rice|0|savoury:2|shareable||
Khaman Dhokla|starter|vegan|2|120|gram flour,mustard seeds|gram flour|1|sweet:2,sour:2|shareable|gujarat|
Khandvi|starter|veg|2|140|gram flour,curd,sesame,coconut|gram flour|1|savoury:2|shareable|gujarat|
Fafda with Jalebi|starter|veg|2|130|gram flour,jalebi|gram flour|0|sweet:3,savoury:2||gujarat|
Shrikhand|dessert|veg|1|90|curd,sugar,pistachio|curd|0|sweet:4||gujarat|
Mohanthal|dessert|veg|1|90|gram flour,ghee,almond|gram flour|0|sweet:4,rich:3||gujarat|
Basundi|dessert|veg|1|100|milk,sugar,almond|milk|0|sweet:4||gujarat|
Chaas|beverage|veg|1|40|curd,cumin|curd|0|sour:2||gujarat|
""",
    "italian": """
Margherita Pizza|meal|veg|2|350|pizza base,mozzarella,tomato,basil|mozzarella|0|savoury:3|shareable||
Farmhouse Pizza|meal|veg|2|420|pizza base,mozzarella,capsicum,onion,mushroom|mushroom|1|savoury:3|shareable||
Paneer Tikka Pizza|meal|veg|2|440|pizza base,mozzarella,paneer,capsicum|paneer|2|smoky:2|shareable||
Chicken BBQ Pizza|meal|non_veg|2|480|pizza base,mozzarella,chicken,onion|chicken|1|smoky:3,sweet:2|shareable||
Pesto Pasta|meal|veg|1|320|pasta,basil,pine nut,cheese|pasta|0|savoury:3|||
Penne Arrabbiata|meal|vegan|1|290|pasta,tomato,chilli,garlic|pasta|3|tangy:3|||
Fettuccine Alfredo|meal|veg|1|330|pasta,cream,cheese,butter|pasta|0|rich:4|||
Chicken Alfredo|meal|non_veg|1|380|pasta,chicken,cream,cheese|pasta,chicken|0|rich:4|||
Veg Lasagna|meal|veg|2|420|pasta,cheese,vegetables,cream|pasta|1|rich:4|shareable||
Mushroom Risotto|meal|veg|1|360|rice,mushroom,cheese,butter|rice,mushroom|0|rich:4|||
Garlic Bread|starter|veg|2|160|bread,butter,garlic|bread|0|savoury:3|shareable||
Cheesy Garlic Bread|starter|veg|2|200|bread,butter,garlic,mozzarella|bread|0|rich:3|shareable||
Bruschetta|starter|vegan|2|180|bread,tomato,basil|bread|0|tangy:2|shareable||
Caesar Salad|side|egg|1|240|lettuce,mayonnaise,cheese,bread|lettuce|0|savoury:2|||
Greek Salad|side|veg|1|230|cucumber,feta,olive,tomato|cucumber|0|tangy:2|||
Minestrone Soup|side|vegan|1|180|vegetables,pasta,tomato|vegetables|1|savoury:2|||
Tiramisu|dessert|egg|1|260|mascarpone,egg,coffee,flour|mascarpone|0|sweet:4,bitter:1|||
Chocolate Brownie|dessert|egg|1|180|chocolate,flour,egg,butter,walnut|chocolate|0|sweet:4,rich:3|bakery||
Eggless Chocolate Cake|dessert|veg|1|190|flour,chocolate,milk|chocolate|0|sweet:4|bakery||
Cold Coffee|beverage|veg|1|160|milk,coffee,sugar|coffee|0|sweet:3,bitter:1|||
Fresh Lime Soda|beverage|vegan|1|90|lime,soda,sugar|lime|0|sour:3,sweet:2|||
""",
}

RESTAURANTS = [  # name, cuisines, kitchen_flags, overrides (the traps live here)
    ("Punjab Grill House", ["north_indian"], [], {}),
    ("Delhi Darbar", ["north_indian", "mughlai"], [], {}),
    ("Amritsari Dhaba", ["north_indian"], [], {"price_level": 0.85}),
    ("The Curry Leaf Bistro", ["north_indian"], [], {"price_level": 1.25}),
    ("Rasoi Express", ["north_indian"], [], {"rating": 3.6}),                        # below the 3.8 cut-off
    ("Nawab's Kitchen", ["mughlai"], [], {}),
    ("Lucknowi Zaika", ["mughlai"], [], {}),
    ("Kebab Factory Outlet", ["mughlai"], [], {"distance_km": 11.5}),                # too far for most deadlines
    ("Paradise Biryani Point", ["biryani"], [], {}),
    ("Bawarchi Hyderabadi", ["biryani"], [], {}),
    ("Biryani Blues", ["biryani"], [], {"price_level": 0.9}),
    ("Shah Ghouse Cafe", ["biryani", "mughlai"], [], {}),
    ("Vidyarthi Bhavan Tiffins", ["south_indian"], [], {"open": "07:00", "close": "15:00"}),  # closed at dinner
    ("MTR Style Tiffin Room", ["south_indian"], ["nut_free_kitchen"], {}),
    ("Kerala Kitchen", ["south_indian"], [], {}),
    ("Chennai Mess", ["south_indian"], [], {"price_level": 0.85}),
    ("Dragon Wok", ["indo_chinese"], ["shared_fryer_nuts"], {}),
    ("Mainland Express", ["indo_chinese"], [], {"price_level": 1.2}),
    ("Chowman Tangra", ["indo_chinese"], ["shared_fryer_nuts"], {}),
    ("Beijing Bites", ["indo_chinese", "north_indian"], [], {}),
    ("Mumbai Chowpatty", ["street_food"], ["shared_fryer_nuts"], {}),
    ("Chandni Chowk Chaat House", ["street_food"], [], {}),
    ("Kathi Junction", ["street_food"], [], {}),
    ("Paratha Wali Gali", ["street_food", "north_indian"], [], {}),
    ("Bhojohori Manna", ["bengali"], [], {}),
    ("Oh! Calcutta Express", ["bengali"], [], {"price_level": 1.2}),
    ("Kasturi Bengali Kitchen", ["bengali"], [], {}),
    ("Rajdhani Thali House", ["gujarati"], [], {"pure_veg": True}),
    ("Swati Snacks", ["gujarati"], [], {"pure_veg": True}),
    ("Marwari Bhojanalaya", ["gujarati"], [], {"pure_veg": True}),
    ("Little Italy Kitchen", ["italian"], [], {"price_level": 1.2}),
    ("Pizza Piazza", ["italian"], [], {}),
    ("Night Owl Kitchen", ["north_indian", "indo_chinese"], [], {"open": "18:00", "close": "23:59"}),
    ("Udupi Sagar", ["south_indian"], [], {"pure_veg": True}),
]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def parse_pool(text: str) -> list[dict]:
    dishes = []
    for line in text.strip().splitlines():
        name, course, diet, serves, price, ings, primary, spice, tastes, tags, famous, may = line.split("|")
        dishes.append({
            "name": name, "course": course, "diet": diet, "serves": float(serves) if "." in serves else int(serves),
            "price": int(price), "ingredients": ings.split(","), "primary": primary.split(",") if primary else [],
            "spice": int(spice), "tastes": {k: int(v) for k, v in (t.split(":") for t in tastes.split(",") if t)},
            "tags": tags.split(",") if tags else [], "famous_in": famous.split(",") if famous else [],
            "may_contain": may.split(",") if may else [],
        })
    return dishes


def derived(ingredients: list[str], table: dict[str, list[str]]) -> set[str]:
    return {a for g in ingredients for a in table.get(g, [])}


def check_diet(d: dict, allergens: set[str]) -> None:
    meat = MEAT & set(d["ingredients"])
    if d["diet"] in ("veg", "vegan", "egg") and meat:
        raise ValueError(f"{d['name']}: {d['diet']} but has {meat}")
    if d["diet"] == "vegan" and (allergens & ANIMAL or {"honey"} & set(d["ingredients"])):
        raise ValueError(f"{d['name']}: vegan but has {allergens & ANIMAL}")
    if d["diet"] == "veg" and "egg" in allergens:
        raise ValueError(f"{d['name']}: veg but has egg")


def build() -> dict:
    rng = random.Random(SEED)
    table = {k: v for k, v in json.loads((HERE / "ingredient_allergens.json").read_text()).items() if not k.startswith("_")}
    pools = {c: parse_pool(t) for c, t in POOLS.items()}
    for c, pool in pools.items():
        for d in pool:
            check_diet(d, derived(d["ingredients"], table))

    restaurants = []
    for n, (name, cuisines, flags, o) in enumerate(RESTAURANTS):
        code = "r_" + slug(name)
        level = o.get("price_level", rng.choice([0.95, 1.0, 1.05, 1.1]))
        rating = o.get("rating", round(rng.uniform(3.9, 4.7), 1))
        pool = [d for c in cuisines for d in pools[c]]
        pool = list({d["name"]: d for d in pool}.values())               # a dish shared by two cuisines once
        if o.get("pure_veg"):
            pool = [d for d in pool if d["diet"] in ("veg", "vegan")]
        if "nut_free_kitchen" in flags:
            pool = [d for d in pool if not derived(d["ingredients"], table) & {"peanut", "tree_nut"}
                    and not set(d["may_contain"]) & {"peanut", "tree_nut"}]
        rng.shuffle(pool)
        size = min(len(pool), rng.randint(20, 30))
        menu = pool[:size]
        veg_mains = [d for d in menu if d["course"] in ("main", "meal") and d["diet"] in ("veg", "vegan")]
        for d in pool[size:]:                                             # every outlet can feed vegetarians
            if len(veg_mains) >= 4:
                break
            if d["course"] in ("main", "meal") and d["diet"] in ("veg", "vegan"):
                menu.append(d); veg_mains.append(d)
        order = ["starter", "main", "meal", "rice", "bread", "noodles", "side", "dessert", "beverage"]
        menu.sort(key=lambda d: (order.index(d["course"]), d["name"]))

        items = []
        for d in menu:
            allergens = derived(d["ingredients"], table)
            item_rating = round(min(4.9, max(3.3, rating + rng.gauss(0, 0.25))), 1)
            item = {
                "id": f"x{n:02d}_{slug(d['name'])}", "name": d["name"], "course": d["course"],
                "price": max(30, int(round(d["price"] * level / 10) * 10)), "serves": d["serves"], "diet": d["diet"],
                "contains": sorted(allergens), "may_contain": sorted(set(d["may_contain"]) - allergens),
                "ingredients": d["ingredients"], "primary": d["primary"], "spice": d["spice"], "tastes": d["tastes"],
                "famous_in": d["famous_in"], "tags": d["tags"], "rating": item_rating,
                "orders_30d": int(rng.uniform(80, 2500) * (item_rating - 3) / 1.5),
            }
            if rng.random() < 0.02:
                item["in_stock"] = False
            if rng.random() < 0.03:
                item["allergen_verified"] = False
            items.append(item)

        restaurants.append({
            "id": code, "name": name, "cuisines": cuisines, "rating": rating, "pure_veg": bool(o.get("pure_veg")),
            "prep_time_min": rng.choice([15, 20, 20, 25, 25, 30, 35]),
            "distance_km": o.get("distance_km", round(rng.uniform(1.5, 7.5), 1)),
            "delivery_fee": rng.choice([20, 25, 30, 35, 40, 45, 50]), "packaging_fee": rng.choice([15, 20, 25, 30, 40]),
            "kitchen_flags": flags, "open": o.get("open", rng.choice(["10:00", "11:00", "11:30", "12:00"])),
            "close": o.get("close", rng.choice(["22:30", "23:00", "23:30", "23:59"])), "items": items,
        })

    # Under-declared allergens: the restaurant's list misses one the recipe implies. Only the
    # database's derived dish_allergen rows catch these, so they are the test for that rule.
    candidates = [i for r in restaurants for i in r["items"] if i["contains"]]
    for item in rng.sample(candidates, 4):
        dropped = rng.choice(item["contains"])
        item["contains"].remove(dropped)
        item["_under_declared"] = dropped
    return {"_generated_by": "python -m foodagent.data.generate_catalog", "restaurants": restaurants}


def main() -> None:
    data = build()
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    n_items = sum(len(r["items"]) for r in data["restaurants"])
    print(f"Wrote {OUT.name}: {len(data['restaurants'])} restaurants, {n_items} dishes.")


if __name__ == "__main__":
    main()

station_data = {
    "stations": [
        {"name": "12th St. Oakland City Center", "abbr": "12TH", "latitude": 37.8037, "longitude": -122.2715},
        {"name": "16th St. Mission", "abbr": "16TH", "latitude": 37.7651, "longitude": -122.4196},
        {"name": "19th St. Oakland", "abbr": "19TH", "latitude": 37.8084, "longitude": -122.2688},
        {"name": "24th St. Mission", "abbr": "24TH", "latitude": 37.7524, "longitude": -122.4181},
        {"name": "Antioch", "abbr": "ANTC", "latitude": 37.9924, "longitude": -121.7804},
        {"name": "Ashby", "abbr": "ASHB", "latitude": 37.8528, "longitude": -122.2701},
        {"name": "Balboa Park", "abbr": "BALB", "latitude": 37.7216, "longitude": -122.4475},
        {"name": "Bay Fair", "abbr": "BAYF", "latitude": 37.6971, "longitude": -122.1268},
        {"name": "Berryessa/North San Jose", "abbr": "BERY", "latitude": 37.3686, "longitude": -121.8747},
        {"name": "Castro Valley", "abbr": "CAST", "latitude": 37.6907, "longitude": -122.0756},
        {"name": "Civic Center/UN Plaza", "abbr": "CIVC", "latitude": 37.7795, "longitude": -122.4138},
        {"name": "Coliseum", "abbr": "COLS", "latitude": 37.7537, "longitude": -122.1968},
        {"name": "Colma", "abbr": "COLM", "latitude": 37.6847, "longitude": -122.4661},
        {"name": "Concord", "abbr": "CONC", "latitude": 37.9735, "longitude": -122.0291},
        {"name": "Daly City", "abbr": "DALY", "latitude": 37.7061, "longitude": -122.4691},
        {"name": "Downtown Berkeley", "abbr": "DBRK", "latitude": 37.8701, "longitude": -122.2681},
        {"name": "Dublin/Pleasanton", "abbr": "DUBL", "latitude": 37.7017, "longitude": -121.8991},
        {"name": "El Cerrito del Norte", "abbr": "DELN", "latitude": 37.9257, "longitude": -122.3172},
        {"name": "El Cerrito Plaza", "abbr": "PLZA", "latitude": 37.9026, "longitude": -122.2989},
        {"name": "Embarcadero", "abbr": "EMBR", "latitude": 37.7932, "longitude": -122.3964},
        {"name": "Fremont", "abbr": "FRMT", "latitude": 37.5574, "longitude": -121.9764},
        {"name": "Fruitvale", "abbr": "FTVL", "latitude": 37.7749, "longitude": -122.2242},
        {"name": "Glen Park", "abbr": "GLEN", "latitude": 37.7331, "longitude": -122.4338},
        {"name": "Hayward", "abbr": "HAYW", "latitude": 37.6694, "longitude": -122.0870},
        {"name": "Lafayette", "abbr": "LAFY", "latitude": 37.8931, "longitude": -122.1247},
        {"name": "Lake Merritt", "abbr": "LAKE", "latitude": 37.7971, "longitude": -122.2652},
        {"name": "MacArthur", "abbr": "MCAR", "latitude": 37.8290, "longitude": -122.2670},
        {"name": "Millbrae", "abbr": "MLBR", "latitude": 37.5998, "longitude": -122.3867},
        {"name": "Milpitas", "abbr": "MLPT", "latitude": 37.4102, "longitude": -121.9087},
        {"name": "Montgomery St.", "abbr": "MONT", "latitude": 37.7894, "longitude": -122.4014},
        {"name": "North Berkeley", "abbr": "NBRK", "latitude": 37.8739, "longitude": -122.2834},
        {"name": "North Concord/Martinez", "abbr": "NCON", "latitude": 38.0032, "longitude": -122.0246},
        {"name": "Oakland International Airport", "abbr": "OAKL", "latitude": 37.7134, "longitude": -122.2122},
        {"name": "Orinda", "abbr": "ORIN", "latitude": 37.8789, "longitude": -122.1838},
        {"name": "Pittsburg Center", "abbr": "PCTR", "latitude": 38.0169, "longitude": -121.8892},
        {"name": "Pittsburg/Bay Point", "abbr": "PITT", "latitude": 38.0189, "longitude": -121.9451},
        {"name": "Pleasant Hill/Contra Costa Centre", "abbr": "PHIL", "latitude": 37.9284, "longitude": -122.0560},
        {"name": "Powell St.", "abbr": "POWL", "latitude": 37.7849, "longitude": -122.4074},
        {"name": "Richmond", "abbr": "RICH", "latitude": 37.9369, "longitude": -122.3531},
        {"name": "Rockridge", "abbr": "ROCK", "latitude": 37.8446, "longitude": -122.2513},
        {"name": "San Bruno", "abbr": "SBRN", "latitude": 37.6378, "longitude": -122.4161},
        {"name": "San Francisco International Airport", "abbr": "SFIA", "latitude": 37.6160, "longitude": -122.3926},
        {"name": "San Leandro", "abbr": "SANL", "latitude": 37.7219, "longitude": -122.1608},
        {"name": "South Hayward", "abbr": "SHAY", "latitude": 37.6348, "longitude": -122.0570},
        {"name": "South San Francisco", "abbr": "SSAN", "latitude": 37.6643, "longitude": -122.4439},
        {"name": "Union City", "abbr": "UCTY", "latitude": 37.5904, "longitude": -122.0170},
        {"name": "Walnut Creek", "abbr": "WCRK", "latitude": 37.9105, "longitude": -122.0672},
        {"name": "Warm Springs/South Fremont", "abbr": "WARM", "latitude": 37.5021, "longitude": -121.9398},
        {"name": "West Dublin/Pleasanton", "abbr": "WDUB", "latitude": 37.6997, "longitude": -121.9281},
        {"name": "West Oakland", "abbr": "WOAK", "latitude": 37.8049, "longitude": -122.2950}
    ]
}

station_groups = {
    "Oakland": [
        "12th St. Oakland City Center",
        "19th St. Oakland",
        "Oakland International Airport",
        "West Oakland"
    ],
    "San Francisco": [
        "San Francisco International Airport",
        "South San Francisco",
    ],
    
    "Mission": [
        "16th St. Mission",
        "24th St. Mission"
    ],
    
    "Berkeley": [
        "Downtown Berkeley",
        "North Berkeley"
    ],
    
    "Dublin": [
        "Dublin/Pleasanton",
        "West Dublin/Pleasanton"
    ],

    "Pleasanton": [
        "Dublin/Pleasanton",
        "West Dublin/Pleasanton"
    ],
    
    "Cerrito": [
        "El Cerrito del Norte",
        "El Cerrito Plaza"
    ],
    
    "Pittsburg": [
        "Pittsburg Center",
        "Pittsburg/Bay Point"
    ],
    
    # "Fremont": [
    #     "Fremont",
    #     "Warm Springs/South Fremont"
    # ],
    
    # "Hayward": [
    #     "Hayward",
    #     "South Hayward"
    # ],
    # "Concord": [
    #     "Concord",
    #     "North Concord/Martinez"
    # ],
    
    "El Cerrito": [
        "El Cerrito del Norte",
        "El Cerrito Plaza"
    ]
}

supported_languages = {
    "en": {
        "transcribe_code": "en-US",
        "polly_voice": "Matthew",
        "name": "English",
        "code": "en",
        "speech_rate": "medium",
        "punctuation": ['.', ',', '!', '?', ':', ';']
    },
    "es": {
        "transcribe_code": "es-US",
        "polly_voice": "Lupe",
        "name": "Spanish",
        "code": "es",
        "speech_rate": "medium",
        "punctuation": ['.', ',', '¡', '!', '¿', '?', ':', ';']
    },
    "zh": {
        "transcribe_code": "zh-CN",
        "polly_voice": "Zhiyu",
        "name": "Chinese",
        "code": "zh",
        "speech_rate": "medium",
        "punctuation": ['。', '，', '！', '？', '：', '；', '.', ',', '!', '?']
    }
}

# User Session Logging Configuration
USER_SESSION_LOGGING_CONFIG = {
    "s3_bucket": "bart-user-session-logs",  # S3 bucket for user session logs (development)
    "enabled": True,  # Enable/disable user session logging
    "store_as_text": True,  # Store logs as simple text files instead of JSON
    "flush_on_close": True  # Flush all logs when websocket closes
}

thinking_messages = {
            "en": [
                "I'm looking into that now...",
                "Let me check that for you...",
                "Just a moment while I get that information...",
                "I'm working on it...",
                "Let me find that for you...",
                "Give me a second to pull that up...",
                "One moment, I'm checking on that...",
                "Let me take a quick look...",
                "Hang on, getting that info for you...",
                "I'll find that out right away...",
                "Checking into that now...",
                "Let me see what I can find...",
                "Just a sec while I look that up...",
                "I'm on it—won't be long...",
                "Let me grab those details for you..."
            ],
            "es": [
                "Estoy investigando eso ahora...",
                "Déjame verificar eso para ti...",
                "Un momento mientras obtengo esa información...",
                "Estoy trabajando en ello...",
                "Déjame buscar eso para ti...",
                "Dame un segundo para mostrarte eso...",
                "Un momento, estoy verificando eso...",
                "Déjame echar un vistazo rápido...",
                "Espera, obteniendo esa información para ti...",
                "Encontraré eso de inmediato...",
                "Verificando eso ahora...",
                "Déjame ver qué puedo encontrar...",
                "Un segundo mientras busco eso...",
                "Estoy en ello, no tardaré...",
                "Déjame obtener esos detalles para ti..."
            ],
            "zh": [
                "我正在查看...",
                "让我为您查一下...",
                "请稍等，我正在获取信息...",
                "我正在处理...",
                "让我为您找一下...",
                "给我一秒钟调出来...",
                "稍等，我正在查看...",
                "让我快速看一下...",
                "稍等，正在为您获取信息...",
                "我马上就能找到...",
                "正在查看...",
                "让我看看能找到什么...",
                "稍等我查一下...",
                "我在处理，很快就好...",
                "让我为您获取这些详细信息..."
            ]
        }

#!/usr/bin/env python3
"""Download US ZIP centroids and add 2020 Census ZCTA population density.
GeoNames attribution: https://www.geonames.org/ (CC BY 4.0).
Census data: https://www2.census.gov/programs-surveys/decennial/2020/data/
Set CENSUS_API_KEY to access the Census population API.
"""
import argparse
import csv
import io
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from zipfile import ZipFile

CENSUS_API = 'https://api.census.gov/data/2020/dec/pl'
ZCTA_GAZETTEER = 'https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2020_Gazetteer/2020_Gaz_zcta_national.zip'


def census_populations(api_key):
    query = urlencode({
        'get': 'P1_001N',
        'for': 'zip code tabulation area:*',
        'key': api_key,
    })
    with urlopen(f'{CENSUS_API}?{query}', timeout=120) as response:
        rows = json.load(response)
    header = rows[0]
    zcta_index = header.index('zip code tabulation area')
    population_index = header.index('P1_001N')
    return {row[zcta_index]: int(row[population_index]) for row in rows[1:]}


def zcta_land_areas():
    with urlopen(ZCTA_GAZETTEER, timeout=120) as response:
        archive_data = response.read()
    with ZipFile(io.BytesIO(archive_data)) as archive:
        filename = archive.namelist()[0]
        text = archive.read(filename).decode('utf-8')
    rows = csv.DictReader(io.StringIO(text), delimiter='\t')
    return {
        row['GEOID'].strip().removeprefix('ZCTA5 '): float(row['ALAND_SQMI'])
        for row in rows
    }


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=Path('data/us_zipcodes.csv'))
    a=p.parse_args()
    api_key = os.environ.get('CENSUS_API_KEY')
    if not api_key:
        p.error('Set CENSUS_API_KEY to download Census population counts')
    populations = census_populations(api_key)
    land_areas = zcta_land_areas()
    with urlopen('https://download.geonames.org/export/zip/US.zip',timeout=120) as response:
        data=response.read()
    rows={}
    with ZipFile(io.BytesIO(data)) as archive:
        for line in archive.read('US.txt').decode('utf-8').splitlines():
            values=line.split('\t')
            if len(values)>=11 and values[0]=='US':
                zipcode=values[1]
                # Repeated place names can share a ZIP; keep one centroid per ZIP.
                rows.setdefault(zipcode, (zipcode,values[9],values[10],values[4]))
    if not rows:
        raise RuntimeError('No postal codes returned')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.writer(stream)
        writer.writerow(['zip','lat','lng','state','population','land_area_sqmi','population_density'])
        for zipcode in sorted(rows):
            population = populations.get(zipcode)
            land_area = land_areas.get(zipcode)
            density = population / land_area if population is not None and land_area else None
            writer.writerow((*rows[zipcode], population, land_area, density))
    print(f'Saved {len(rows)} distinct ZIP coordinates with 2020 Census population data to {a.output}')


if __name__=='__main__':
    main()

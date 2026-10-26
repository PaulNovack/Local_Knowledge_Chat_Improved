import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from openpyxl import Workbook
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from ingest_locations import DEFAULT_DB,ingest,parse_hours,DownloadForms

class TestImporter(unittest.TestCase):
    def test_hours(self):
        rules=parse_hours('Mon-Fri: 6:00 AM - 12:00 AM, Sat: 7:00 AM - 10:00 PM, Sun: 7:00 AM - 10:00 PM')
        self.assertEqual(len(rules),2)
        self.assertEqual(rules[0]['days'],[0,1,2,3,4])
        self.assertEqual(rules[0]['closes_next_day'],1)
        self.assertEqual(rules[1]['days'],[5,6])
        self.assertEqual(parse_hours('Mon-Sun: 12:00 AM - 12:00 AM')[0]['parse_status'],'interpreted_24_hours')
        self.assertFalse(parse_hours('Mon-Sun: 12:00 AM - 12:00 PM')[0]['is_24_hours'])
        self.assertEqual(parse_hours('Mon-Fri: 8:00 AM - 12:00 AM, Sat-Sun: Closed')[1]['is_closed'],1)
        self.assertEqual(parse_hours('Yes')[0]['parse_status'],'unknown')
        self.assertEqual(parse_hours(None)[0]['parse_status'],'missing')
    def test_download_discovery(self):
        p=DownloadForms();p.feed('<form action="/api/locationsdata/download/" method="get"><button>Download Location Data</button></form>')
        self.assertEqual(p.forms[0]['action'],'/api/locationsdata/download/')
    def test_default_database_is_app_database(self):
        self.assertEqual(DEFAULT_DB,Path(__file__).resolve().parents[2]/'database'/'chat_history.sqlite3')
    def test_import_preserves_existing_app_data(self):
        headers=['Site ID','Location ID','Brand','State','City','Location','Address','Latitude','Longitude']
        values=['0001','1001','TA','TX','Houston','TA Travel Center','1 Main St',29.7,-95.3]
        with tempfile.TemporaryDirectory() as tmp:
            workbook_path=Path(tmp)/'locations.xlsx'
            workbook=Workbook()
            sheet=workbook.active
            sheet.append(headers)
            sheet.append(values)
            workbook.save(workbook_path)
            db=Path(tmp)/'chat_history.sqlite3'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE chat_history (ID INTEGER PRIMARY KEY, question TEXT)')
                conn.execute("INSERT INTO chat_history(question) VALUES ('keep this row')")
            self.assertEqual(ingest(workbook_path,db),1)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute('SELECT question FROM chat_history').fetchone()[0],'keep this row')
                self.assertEqual(conn.execute('SELECT site_id FROM ta_locations').fetchone()[0],'0001')
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
    def test_repeat_import(self):
        source=Path(__file__).resolve().parents[1]/'data/location_master.xlsx'
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'locations.sqlite3'
            first=ingest(source,db)
            with sqlite3.connect(db) as c:
                before={t:c.execute('select count(*) from '+t).fetchone()[0] for t in ['ta_locations','ta_facilities','ta_contacts','ta_location_amenities','ta_hours','ta_hour_days']}
            self.assertEqual(ingest(source,db),first)
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('select count(*) from ta_locations').fetchone()[0],first)
                for t,count in before.items():self.assertEqual(c.execute('select count(*) from '+t).fetchone()[0],count)
                self.assertEqual(c.execute("select postal_code from ta_locations where site_id='0006'").fetchone()[0],'07832-0305')
                self.assertEqual(c.execute("select location_id from ta_locations where site_id='0487'").fetchone()[0],None)
                self.assertEqual(c.execute('select count(*) from ta_source_columns').fetchone()[0],97)
                self.assertEqual(c.execute("select count(*) from ta_source_columns where header='Interstate Speedzone Wifi'").fetchone()[0],2)
                self.assertEqual(c.execute('pragma integrity_check').fetchone()[0],'ok')
                self.assertEqual(c.execute('pragma foreign_key_check').fetchall(),[])

if __name__=='__main__':unittest.main()

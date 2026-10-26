import sys
import sqlite3
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from scrape_roadys import DEFAULT_DB,migrate,validate,save_location,parse_hours

ROW={'locationid':1,'locationname':'Test Stop','locationaddress':'1 Main St','locationcity':'Test City',
     'locationstate':'TX','locationzip':'00123','locationlat':30.1,'locationlong':-95.2,
     'locationtype':'R','locationgroup':'R','has_rewards':True,'diesel_price':3.899,
     'locationphonenumber':'(800) 555-0100','amenities':[
         {'id':24,'name':'Parking Spaces','value':'20+'},
         {'id':224,'name':'Store Hours','value':'M-Sat 5am-11pm, Sun 6am-10pm'},
         {'id':99,'name':'Accepted Fuel Cards','value':'EFS,Comdata'},
         {'id':38,'name':'Food Selection','value':'Pizza,Burgers'}]}

class Tests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.execute('pragma foreign_keys=on')
        migrate(self.c);migrate(self.c)
    def tearDown(self):self.c.close()
    def test_default_database_and_schema_creation(self):
        self.assertEqual(DEFAULT_DB,Path(__file__).resolve().parents[2]/'database'/'chat_history.sqlite3')
        objects={row[0] for row in self.c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'roadys_%'")}
        self.assertTrue({'roadys_locations','roadys_runs','roadys_hours_grouped'}<=objects)
    def test_repeat_import_and_relations(self):
        for _ in range(2):
            with self.c:save_location(self.c,ROW,'2026-10-25T00:00:00Z',1,'test')
        self.assertEqual(self.c.execute('select count(*) from roadys_locations').fetchone()[0],1)
        self.assertEqual(self.c.execute('select count(*) from roadys_location_payments').fetchone()[0],2)
        self.assertEqual(self.c.execute('select price_mills from roadys_fuel_prices').fetchone()[0],3899)
        self.assertEqual(self.c.execute('select postal_code from roadys_locations').fetchone()[0],'00123')
        self.assertEqual(self.c.execute('select numeric_value,numeric_is_minimum from roadys_location_amenities where amenity_id=24').fetchone(),(20.0,1))
        self.assertFalse(self.c.execute('pragma foreign_key_check').fetchall())
    def test_hours(self):
        rules=parse_hours('M-Sat 5am-11pm, Sun 6am-10pm')
        self.assertEqual(rules[0]['days'],list(range(6)))
        self.assertEqual(rules[1]['days'],[6])
        self.assertEqual(parse_hours('5:am-11:pm')[0]['opens_at'],'05:00')
        self.assertEqual(parse_hours('5:am-11:pm')[0]['day_scope'],'unspecified')
        self.assertEqual(parse_hours('10pm-2am')[0]['closes_next_day'],1)
        self.assertEqual(parse_hours('24/7')[0]['is_24_hours'],1)
        self.assertEqual(parse_hours('SAME')[0]['parse_status'],'reference_unresolved')
        self.assertEqual(parse_hours('24/7 Truck Side Grocery 6AM-10PM')[0]['parse_status'],'unparsed')
    def test_compound_hours_and_references(self):
        rules=parse_hours('M-Th 5am-9pm Fri & Sat 5am-10pm Sun 6am-9pm')
        self.assertEqual([r['days'] for r in rules],[[0,1,2,3],[4,5],[6]])
        self.assertEqual(parse_hours('5am to 10pm Sun - Thur / 5am - 11pm Fri & Sat')[0]['days'],[0,1,2,3,6])
        row=dict(ROW,amenities=ROW['amenities']+[
            {'id':300,'name':'Service Hours','value':'8AM-5PM M-F'},
            {'id':301,'name':'Parts Hours','value':'SAME'}])
        with self.c:save_location(self.c,row,'first',1,'test')
        self.assertEqual(self.c.execute("select opens_at,parse_status from roadys_hours where source_amenity='Parts Hours'").fetchone(),('08:00','resolved_reference'))
        self.assertEqual(self.c.execute('select referenced_amenity from roadys_hour_references').fetchone()[0],'Service Hours')

    def test_source_identity_validation(self):
        rows,_=validate({'success':True,'data':{'locations':[ROW,ROW]}})
        self.assertEqual(len(rows),1)
        with self.assertRaises(ValueError):validate({'success':True,'data':{'locations':[]}})
        with self.assertRaises(ValueError):validate({'success':True,'data':{'locations':[ROW,dict(ROW,locationname='Conflicting name')]}})
    def test_transaction_rollback(self):
        with self.c:save_location(self.c,ROW,'first',1,'test')
        with self.assertRaises(sqlite3.IntegrityError):
            with self.c:save_location(self.c,dict(ROW,locationname=None),'second',2,'test')
        self.assertEqual(self.c.execute('select name from roadys_locations').fetchone()[0],'Test Stop')

if __name__=='__main__':unittest.main()

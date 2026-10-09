"""Run with: uv run --with pyarrow python -m unittest discover -s scripts -p 'test_*.py'."""
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import pyarrow.parquet as pq

from convert_ktru_xml import DATA, POSITION, convert, field_mapping, parse

XML = '''<export xmlns="http://zakupki.gov.ru/oos/export/1" xmlns:o="http://zakupki.gov.ru/oos/types/1" xmlns:c="http://zakupki.gov.ru/oos/common/1"><nsiKTRUs>
<o:position><o:data><o:code>32.50.13.190-00016</o:code><o:version>2</o:version><o:name> Name \t</o:name><o:status>TERMINATED</o:status><o:publishDate>2026-10-02T12:57:47+03:00</o:publishDate><o:noNewFeatures>false</o:noNewFeatures><o:OKEIs/>
<o:characteristics><o:characteristic><o:code>characteristic-id</o:code><o:name> Volume </o:name><o:type>2</o:type><o:kind>1</o:kind><o:actual>true</o:actual><o:isRequired>true</o:isRequired><o:choiceType>1</o:choiceType><o:values><o:value><o:valueCode>value-id</o:valueCode><o:OKEI><o:code>003</o:code><o:name>Millimetre</o:name></o:OKEI><o:rangeSet><o:valueRange><o:minMathNotation>greaterOrEqual</o:minMathNotation><o:min>0.100000000000000001</o:min><o:maxMathNotation>lessOrEqual</o:maxMathNotation><o:max>1.50</o:max></o:valueRange></o:rangeSet><o:valueSet><o:concreteValue>001</o:concreteValue><o:concreteValue>2.00</o:concreteValue></o:valueSet></o:value></o:values></o:characteristic></o:characteristics>
<o:NSI><o:standarts><o:standart><o:docName>Standard</o:docName></o:standart></o:standarts></o:NSI><o:nsiDescription/><o:parentPositionInfo><o:code>parent</o:code><o:version>1</o:version><o:externalCode>external-parent</o:externalCode></o:parentPositionInfo><o:cancelInfo><o:cancelDate>2026-10-01T00:00:00+03:00</o:cancelDate><o:cancelReason>Reason</o:cancelReason></o:cancelInfo>
<o:attachments><o:attachment><o:fileName>file.pdf</o:fileName><o:fileSize>00123</o:fileSize><o:docDescription>Document</o:docDescription><o:docDate>2026-10-01</o:docDate><o:url>https://example.com/file</o:url></o:attachment></o:attachments>
</o:data><o:printForm><c:url>https://example.com/print</c:url><c:signatureInfo><c:dsUid>signature</c:dsUid></c:signatureInfo></o:printForm></o:position>
</nsiKTRUs></export>'''


def archive(path, actual):
    with zipfile.ZipFile(path, 'w') as outer:
        for kind, data in [('actual', actual), ('not-actual', XML.replace('TERMINATED', 'INACTIVE'))]:
            memory = io.BytesIO()
            with zipfile.ZipFile(memory, 'w') as inner:
                inner.writestr(f'nsiKTRUNew_all_{kind}_20261003020003_001.xml', data)
            outer.writestr(f'{kind}.zip', memory.getvalue())


class ConvertTests(unittest.TestCase):
    def test_nested_roundtrip_scope_and_conflicts(self):
        root = ET.fromstring(XML)
        container = root[0]
        other = ET.fromstring(ET.tostring(container[0]))
        other.find('{http://zakupki.gov.ru/oos/types/1}data/{http://zakupki.gov.ru/oos/types/1}name').text = 'Conflicting name'
        container.append(other)
        retained = ET.fromstring(ET.tostring(container[0]))
        retained.find('{http://zakupki.gov.ru/oos/types/1}data/{http://zakupki.gov.ru/oos/types/1}code').text = 'retained-code'
        container.append(retained)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.zip'
            output = Path(directory) / 'actual.parquet'
            archive(source, ET.tostring(root))
            meta = convert(source, output, batch_size=1)
            rows = pq.read_table(output).to_pylist()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['code'], 'retained-code')
            self.assertEqual(rows[0]['source_row'], 3)
            self.assertEqual(meta['skippedHistoricalPartCount'], 1)
            self.assertEqual(meta['statuses'], {'TERMINATED': 1})
            self.assertEqual(meta['uniqueCodeCount'], 1)
            self.assertEqual(meta['sourceRecordCount'], 3)
            self.assertEqual(meta['excludedRecordCount'], 2)
            self.assertEqual(meta['duplicateCodeVersions'], [])
            self.assertTrue(meta['excludedConflictingCodeVersions'][0]['conflicting'])
            self.assertEqual(rows[0]['name'], ' Name \t')
            self.assertEqual(rows[0]['units'], [])
            self.assertIsNone(rows[0]['rubricators'])
            self.assertEqual(rows[0]['description'], '')
            self.assertFalse(rows[0]['no_new_features'])
            self.assertEqual(rows[0]['publish_date'], '2026-10-02T12:57:47+03:00')
            value = rows[0]['characteristics'][0]['values'][0]
            self.assertEqual(value['unit']['code'], '003')
            self.assertEqual(value['ranges'][0]['min'], '0.100000000000000001')
            self.assertEqual(value['ranges'][0]['max'], '1.50')
            self.assertEqual(value['ranges'][0]['max_operator'], 'lessOrEqual')
            self.assertEqual(value['concrete_values'], ['001', '2.00'])
            self.assertEqual(rows[0]['nsi']['standards'][0]['document_name'], 'Standard')
            self.assertEqual(rows[0]['parent']['external_code'], 'external-parent')
            self.assertEqual(rows[0]['attachments'][0]['file_size'], '00123')
            self.assertEqual(rows[0]['print_form']['signature']['ds_uid'], 'signature')
            with gzip.open(str(output) + '.meta.json.gz', 'rt') as stream:
                self.assertTrue(json.load(stream)['validated'])

    def test_exact_duplicates_are_not_conflicts(self):
        root = ET.fromstring(XML)
        root[0].append(ET.fromstring(ET.tostring(root[0][0])))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.zip'
            output = Path(directory) / 'actual.parquet'
            archive(source, ET.tostring(root))
            meta = convert(source, output, batch_size=1)
            self.assertEqual(pq.read_table(output).num_rows, 2)
            self.assertEqual(meta['excludedRecordCount'], 0)
            self.assertEqual(meta['excludedConflictingCodeVersions'], [])
            self.assertFalse(meta['duplicateCodeVersions'][0]['conflicting'])

    def test_unknown_fields_and_repeated_scalars_rejected(self):
        for xml in ['<o:surprise xmlns:o="http://zakupki.gov.ru/oos/types/1"/>', '<o:code xmlns:o="http://zakupki.gov.ru/oos/types/1">x</o:code><o:code xmlns:o="http://zakupki.gov.ru/oos/types/1">y</o:code>']:
            with self.assertRaises(ValueError):
                parse(ET.fromstring(f'<o:data xmlns:o="http://zakupki.gov.ru/oos/types/1">{xml}</o:data>'), DATA)

    def test_metadata_mapping_uses_actual_parquet_paths(self):
        paths = {v['parquet'] for v in field_mapping(POSITION)}
        self.assertIn('characteristics[].values[].ranges[].max', paths)
        self.assertIn('characteristics[].values[].concrete_values[]', paths)
        self.assertIn('print_form.signature.ds_uid', paths)


if __name__ == '__main__':
    unittest.main()

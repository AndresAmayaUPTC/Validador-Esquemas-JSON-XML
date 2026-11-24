"""Esquemas XSD compartidos por las pruebas de XML."""
from pathlib import Path

_CABECERA = '<?xml version="1.0" encoding="UTF-8"?>\n<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'


def xsd(cuerpo: str) -> bytes:
    return (_CABECERA + cuerpo + '\n</xs:schema>\n').encode('utf-8')


PEDIDO = (Path(__file__).parent / 'xsd' / 'pedido.xsd').read_bytes()

ESQUEMAS = {
    'pedido': PEDIDO,

    # recursión por tipo con nombre y por ref a un elemento global
    'arbol': xsd('''
  <xs:element name="arbol" type="Nodo"/>
  <xs:complexType name="Nodo">
    <xs:sequence>
      <xs:element name="valor" type="xs:int"/>
      <xs:element name="hijo" type="Nodo" minOccurs="0" maxOccurs="3"/>
    </xs:sequence>
    <xs:attribute name="etiqueta" type="xs:string"/>
  </xs:complexType>
  <xs:element name="lista">
    <xs:complexType>
      <xs:choice minOccurs="0" maxOccurs="unbounded">
        <xs:element name="dato" type="xs:decimal"/>
        <xs:element ref="lista"/>
      </xs:choice>
    </xs:complexType>
  </xs:element>'''),

    # secuencias y elecciones anidadas con ocurrencias
    'modelos': xsd('''
  <xs:element name="r">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string" minOccurs="0"/>
        <xs:choice minOccurs="1" maxOccurs="2">
          <xs:element name="b" type="xs:boolean"/>
          <xs:sequence>
            <xs:element name="c" type="xs:integer"/>
            <xs:element name="d" type="xs:integer" minOccurs="2" maxOccurs="4"/>
          </xs:sequence>
        </xs:choice>
        <xs:sequence minOccurs="0" maxOccurs="unbounded">
          <xs:element name="e"><xs:complexType/></xs:element>
          <xs:element name="a" type="xs:string"/>
        </xs:sequence>
        <xs:element name="fin"><xs:complexType><xs:sequence/></xs:complexType></xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>'''),

    # tipos simples con facetas, en elementos y en atributos
    'valores': xsd('''
  <xs:simpleType name="Codigo">
    <xs:restriction base="xs:string"><xs:length value="4"/></xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="Nota">
    <xs:restriction base="xs:decimal"><xs:minExclusive value="0"/><xs:maxInclusive value="5.0"/></xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="NotaAlta">
    <xs:restriction base="Nota"><xs:minInclusive value="3.5"/></xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="Dado">
    <xs:restriction base="xs:integer">
      <xs:enumeration value="1"/><xs:enumeration value="2"/><xs:enumeration value="3"/>
      <xs:enumeration value="4"/><xs:enumeration value="5"/><xs:enumeration value="6"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="Color">
    <xs:restriction base="xs:string">
      <xs:enumeration value="rojo"/><xs:enumeration value="verde"/><xs:enumeration value="azul claro"/>
      <xs:enumeration value=""/>
    </xs:restriction>
  </xs:simpleType>
  <xs:element name="ficha">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="codigo" type="Codigo"/>
        <xs:element name="nota" type="Nota" maxOccurs="unbounded"/>
        <xs:element name="alta" type="NotaAlta" minOccurs="0"/>
        <xs:element name="dado" type="Dado"/>
        <xs:element name="color" type="Color"/>
        <xs:element name="activo" type="xs:boolean"/>
        <xs:element name="octeto" type="xs:unsignedByte"/>
        <xs:element name="grande" type="xs:long"/>
        <xs:element name="resumen">
          <xs:simpleType><xs:restriction base="xs:string"><xs:minLength value="2"/><xs:maxLength value="10"/></xs:restriction></xs:simpleType>
        </xs:element>
      </xs:sequence>
      <xs:attribute name="id" type="xs:nonNegativeInteger" use="required"/>
      <xs:attribute name="color" type="Color" use="required"/>
      <xs:attribute name="peso" type="Nota"/>
      <xs:attribute name="visible" type="xs:boolean"/>
      <xs:attribute name="alias"><xs:simpleType><xs:restriction base="xs:string"><xs:maxLength value="5"/></xs:restriction></xs:simpleType></xs:attribute>
      <xs:attribute name="libre"/>
    </xs:complexType>
  </xs:element>'''),

    # anyType, contenido simple con atributos y herencia de atributos
    'mixtos': xsd('''
  <xs:complexType name="Medida">
    <xs:simpleContent>
      <xs:extension base="xs:decimal"><xs:attribute name="unidad" type="xs:string" use="required"/></xs:extension>
    </xs:simpleContent>
  </xs:complexType>
  <xs:complexType name="MedidaFechada">
    <xs:simpleContent>
      <xs:extension base="Medida"><xs:attribute name="anio" type="xs:positiveInteger"/></xs:extension>
    </xs:simpleContent>
  </xs:complexType>
  <xs:element name="registro">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="medida" type="Medida" maxOccurs="3"/>
        <xs:element name="fechada" type="MedidaFechada" minOccurs="0"/>
        <xs:element name="extra" minOccurs="0"/>
        <xs:element name="cualquiera" type="xs:anyType" minOccurs="0"/>
        <xs:element name="vacio"><xs:complexType><xs:attribute name="marca" type="xs:boolean"/></xs:complexType></xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
  <xs:element name="nota" type="xs:string"/>'''),

    # pattern: AFD anclado. Dos pattern del mismo paso se combinan con «o» (Codigo) y los de pasos distintos
    # con «y» (CodigoCorto); en un entero se aplica sin los espacios de los lados; también en un atributo
    'patron': xsd(r'''
  <xs:simpleType name="Codigo">
    <xs:restriction base="xs:string"><xs:pattern value="[A-Z]{2}-\d{3}"/><xs:pattern value="X+"/></xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="CodigoCorto">
    <xs:restriction base="Codigo"><xs:maxLength value="6"/><xs:pattern value="[^9]*"/></xs:restriction>
  </xs:simpleType>
  <xs:element name="usuario">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="correo">
          <xs:simpleType><xs:restriction base="xs:string"><xs:pattern value="[^@\s]+@[^@\s]+"/><xs:maxLength value="40"/></xs:restriction></xs:simpleType>
        </xs:element>
        <xs:element name="edad">
          <xs:simpleType><xs:restriction base="xs:integer"><xs:pattern value="\d{1,2}"/></xs:restriction></xs:simpleType>
        </xs:element>
        <xs:element name="codigo" type="CodigoCorto" minOccurs="0" maxOccurs="3"/>
      </xs:sequence>
      <xs:attribute name="pais" use="required">
        <xs:simpleType><xs:restriction base="xs:string"><xs:pattern value="\p{Lu}{2}"/></xs:restriction></xs:simpleType>
      </xs:attribute>
    </xs:complexType>
  </xs:element>'''),

    # pattern que no se compila (un bloque Unicode): queda en verificación diferida
    'patron_diferido': xsd(r'''
  <xs:element name="nota">
    <xs:simpleType><xs:restriction base="xs:string"><xs:pattern value="\p{IsBasicLatin}+"/><xs:maxLength value="20"/></xs:restriction></xs:simpleType>
  </xs:element>'''),
}

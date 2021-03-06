from flask import g, current_app
import sqlalchemy.orm.exc

from .models import CertificateAuthority
from commandment.models import db, Device
from commandment.pki.models import CertificateType, Certificate


def get_ca() -> CertificateAuthority:
    if 'ca' not in g:
        try:
            ca = db.session.query(CertificateAuthority).filter_by(common_name='COMMANDMENT-CA').one()
        except sqlalchemy.orm.exc.NoResultFound:
            ca = CertificateAuthority.create()

        g.ca = ca

    return g.ca

#
# @current_app.teardown_appcontext
# def teardown_ca():
#     ca = g.pop('ca', None)


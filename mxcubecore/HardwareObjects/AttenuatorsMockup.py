from HardwareRepository.BaseHardwareObjects import Device

class AttenuatorsMockup(Device):
    def __init__(self, *args):
        Device.__init__(self, *args)
        self.value = 0
        self.emit('attFactorChanged', self.value)

    def getAttState(self):
        return 0

    def getAttFactor(self):
        return self.get_value()

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value
        self.emit('attFactorChanged', self.value)

    def setTransmission(self, value):  
        self.set_value(value)
